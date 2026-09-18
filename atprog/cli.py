"""Kommandozeile von atprog."""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

from . import layout as layout_mod
from .device import (decode, field_write_allowed, patch_channels, patch_settings,
                     read_image, rebuild_contact_index, verify_layout, write_image)
from .image import MemoryImage
from .models import Codeplug
from .protocol import Radio
from .serialport import describe_backend, list_ports
from .version import APP_TITLE, __version__

BAR_WIDTH = 32


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _progress_printer():
    """Fortschrittsanzeige.

    Im Terminal eine Zeile, die sich selbst ueberschreibt; wird die Ausgabe
    umgeleitet oder mitgeschnitten, nur alle 10 Prozent eine kurze Meldung -
    sonst entstuende eine Flut von Zeilen.
    """
    state = {"last": 0.0, "step": -1, "start": time.monotonic(), "base": None}
    interactive = sys.stderr.isatty()

    def remaining(done: int, total: int) -> str:
        """Restzeit aus der tatsaechlich gemessenen Geschwindigkeit."""
        if state["base"] is None:
            state["base"] = done
        moved = done - state["base"]
        elapsed = time.monotonic() - state["start"]
        if moved <= 0 or elapsed < 1.0:
            return ""
        rate = moved / elapsed
        left = (total - done) / rate
        if left < 60:
            return " noch %2ds" % int(left)
        if left < 5400:
            return " noch %2d min" % round(left / 60.0)
        return " noch %.1f h" % (left / 3600.0)

    def fn(done: int, total: int, label: str) -> None:
        frac = done / total if total else 0
        if not interactive:
            step = int(frac * 10)
            if step == state["step"] and done < total:
                return
            state["step"] = step
            sys.stderr.write("  %3d%%  %s%s\n" % (int(frac * 100), label,
                                                  remaining(done, total)))
            sys.stderr.flush()
            return
        # Nur neu zeichnen, wenn sich die Prozentzahl aendert (oder alle 2 s) -
        # sonst ueberschwemmt die Ausgabe jedes Terminalprotokoll.
        now = time.monotonic()
        percent = int(frac * 100)
        if done < total and (percent == state["step"] or now - state["last"] < 0.3):
            return
        state["step"] = percent
        state["last"] = now
        filled = int(BAR_WIDTH * frac)
        sys.stderr.write("\r[%s%s] %3d%% %-9s %-22s" % (
            "#" * filled, "." * (BAR_WIDTH - filled), int(frac * 100),
            remaining(done, total), label[:22]))
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")
    return fn


def _wait_for_port(seconds: float = 15.0) -> Optional[str]:
    """Wartet, bis ein Port auftaucht.

    Nach jedem Befehl startet das Funkgeraet neu; der USB-Port verschwindet
    dabei fuer einige Sekunden. Statt sofort abzubrechen, warten wir kurz.
    """
    deadline = time.monotonic() + seconds
    announced = False
    while True:
        ports = list_ports()
        if ports:
            if announced:
                sys.stderr.write("\n")
            return ports[0]
        if time.monotonic() > deadline:
            if announced:
                sys.stderr.write("\n")
            return None
        if not announced:
            sys.stderr.write("Warte auf das Funkgeraet (startet nach dem letzten "
                             "Befehl neu) ")
            announced = True
        sys.stderr.write(".")
        sys.stderr.flush()
        time.sleep(1.0)


def _connect(args) -> Radio:
    port = args.port
    if not port:
        port = _wait_for_port()
        if not port:
            raise SystemExit("Kein serieller Port gefunden. Funkgeraet einschalten und "
                             "per USB verbinden, dann 'atprog ports' pruefen.")
        _log("Verwende Port %s" % port)
    radio = Radio(port, block_size=args.block, timeout=args.timeout, log=_log,
                  write_block_size=getattr(args, "write_block", 16))
    radio.open()
    return radio


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SystemExit("Abbruch: Bestaetigung noetig (--yes verwenden).")
    answer = input("%s [ja/nein] " % question).strip().lower()
    return answer in ("j", "ja", "y", "yes")


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------

def cmd_ports(args) -> int:
    ports = list_ports()
    print("Serieller Backend: %s" % describe_backend())
    if not ports:
        print("Keine Ports gefunden.")
        return 1
    for p in ports:
        print("  %s" % p)
    return 0


def cmd_info(args) -> int:
    radio = _connect(args)
    try:
        info = radio.info
        print("Modell    : %s" % info.model)
        print("Firmware  : %s" % info.version)
        print("Kennung   : %s" % info.raw.hex(" "))
        lay = layout_mod.load_for(info.model)
        print("Adressplan: %s (%s, Revision %d)" % (lay.id, lay.title, lay.revision))
        if not lay.matches(info.model):
            print("ACHTUNG   : Kein Layout passt exakt zu dieser Kennung.")
    finally:
        radio.close()
    return 0


# Bereiche, die fuer die Fehlersuche am Kontaktspeicher interessant sind:
# Codeplug-Umfeld samt Kontakten und die Zone, in der bei der D868-Familie eine
# Zuordnungstabelle vermutet wird.
SNAPSHOT_RANGES = (
    (0x01000000, 0x00040000),      # Zonen-Kanallisten
    (0x01080000, 0x00040000),      # Scanlisten
    (0x02400000, 0x00300000),      # Bitmaps, Einstellungen, Namen, Kontakte
)


LAB_ADDR = 0x02940000        # benutzt, aber nur mit dem DTMF-Werkseintrag belegt


ZIELE = (
    (0x02540000, "Zonenname 1"),
    (0x02580000, "Radio-ID 1"),
    (0x00800020, "Kanal 1, zweite Haelfte des Datensatzes"),
    (0x02500000, "Grundeinstellungen, erste 16 Byte"),
    (0x02950000, "DTMF-Block, leere Stelle"),
)


ZIELE = (
    (0x02540000, "Zonenname 1"),
    (0x02580000, "Radio-ID 1"),
    (0x02950000, "DTMF-Block, leere Stelle"),
)


def cmd_writelab(args) -> int:
    """Prueft die Puffer-Hypothese: schreiben, END, neu verbinden, pruefen.

    Entscheidend ist, waehrend der Schreibsitzung NICHT zu lesen - das Geraet
    uebernimmt gepufferte Daten erst beim Beenden der Sitzung.
    """
    marke = ("AT%s" % time.strftime("%H%M%S")).encode()      # 8 Byte
    radio = _connect(args)
    geschrieben = []
    try:
        print("Sitzung 1: nur schreiben, nicht lesen")
        for addr, name in ZIELE:
            if args.only and ("0x%08X" % addr).lower() != args.only.lower():
                continue
            alt = radio.read_region(addr, 16)      # noch vor dem ersten Schreiben
            geschrieben.append((addr, name, marke + alt[len(marke):]))
        for addr, name, daten in geschrieben:
            radio.write_block(addr, daten)
            print("  0x%08X  %s geschrieben" % (addr, name))
        print("Beende die Sitzung (END) und warte auf den Neustart ...")
        radio.reopen()
        print("Sitzung 2: pruefen")
        erfolg = []
        for addr, name, daten in geschrieben:
            zurueck = radio.read_region(addr, 16)
            ok = zurueck == daten
            erfolg.append(ok)
            print("  0x%08X  %-26s %s"
                  % (addr, name, "ANGEKOMMEN" if ok else "nicht angekommen"))
    finally:
        radio.close()
    print()
    if any(erfolg):
        print("Das Geraet uebernimmt Schreibzugriffe beim Sitzungsende.")
        print("Lesen waehrend der Schreibsitzung verwirft den Puffer - genau das")
        print("war der Fehler in atprog.")
        return 0
    print("Auch so ist nichts angekommen.")
    return 1


def cmd_snapshot(args) -> int:
    """Liest die fuer die Fehlersuche interessanten Bereiche am Stueck.

    Gedacht fuer den Vergleich vorher/nachher: Schnappschuss nehmen, am Geraet
    etwas aendern, zweiten Schnappschuss nehmen, dann 'atprog diff'.
    """
    radio = _connect(args)
    img = MemoryImage({"purpose": "snapshot",
                       "model": radio.info.model if radio.info else "",
                       "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    try:
        prog = _progress_printer()
        gesamt = sum(size for _, size in SNAPSHOT_RANGES)
        fertig = 0
        for addr, size in SNAPSHOT_RANGES:
            _log("Lese 0x%08X (%d KB)" % (addr, size // 1024))
            data = radio.read_region(addr, size, progress=prog,
                                     label="0x%08X" % addr, done=fertig, total=gesamt)
            img.write(addr, data)
            fertig += size
    finally:
        radio.close()
    img.save(args.out)
    print("Schnappschuss: %s (%d Byte)" % (args.out, img.size()))
    return 0


def cmd_zones(args) -> int:
    """Zonen am Geraet sichtbar oder versteckt schalten."""
    from .codec import bitmap_get
    from .device import set_zone_visibility
    radio = _connect(args)
    try:
        lay = layout_mod.load_for(radio.info.model)
        bitmap = lay.region("zone_bitmap")
        hide = lay.region("zone_hide")
        belegt = radio.read_region(bitmap.addr, bitmap.size)
        versteckt = radio.read_region(hide.addr, hide.size)
        obj = lay.obj("zone_name")
        zonen = []
        for idx in range(obj.count):
            if not bitmap_get(belegt, idx):
                continue
            roh = radio.read_region(obj.address(idx), obj.record_size)
            # Im Bitmap steht hinter den belegten Plaetzen geloeschtes Flash
            # (lauter Einsen). Nur was auch einen lesbaren Namen hat, ist eine
            # echte Zone.
            if set(roh[:16]) <= {0xFF} or set(roh[:16]) <= {0x00}:
                continue
            name = roh[:16].split(b"\x00")[0].decode("latin-1", "replace")
            if not name.strip():
                continue
            zonen.append((idx, name, bitmap_get(versteckt, idx)))
        print("Zonen im Geraet:")
        for idx, name, ist_versteckt in zonen:
            print("  %2d  %-18s %s" % (idx + 1, name, "versteckt" if ist_versteckt else "sichtbar"))
        if not args.show_all and not args.hide_all:
            return 0
        wunsch = {idx: args.show_all for idx, _, _ in zonen}
        img = MemoryImage()
        img.write(hide.addr, radio.read_region(hide.addr, hide.size))
        neu = set_zone_visibility(img, wunsch, lay, log=_log)
        if not img.diff(neu):
            print("\nNichts zu tun - alle Zonen stehen schon so.")
            return 0
        print("\nSchreibe die Sichtbarkeit (%s) ..."
              % ("alle sichtbar" if args.show_all else "alle versteckt"))
        report = write_image(radio, neu, differential=False, verify=True,
                             progress=_progress_printer(), log=_log)
    finally:
        radio.close()
    print(report.summary())
    return 1 if report.mismatches else 0


def cmd_status(args) -> int:
    """Schneller Blick ins Geraet: was ist belegt?"""
    from .codec import bitmap_indices
    radio = _connect(args)
    try:
        lay = layout_mod.load(args.layout) if args.layout else layout_mod.load_for(radio.info.model)
        print("Modell    : %s (Firmware %s)" % (radio.info.model, radio.info.version))
        paare = (("channel_bitmap", "channel", "Kanaele"),
                 ("zone_bitmap", "zone_name", "Zonen"),
                 ("scanlist_bitmap", "scan_list", "Scanlisten"),
                 ("radioid_bitmap", "radio_id", "Radio-IDs"),
                 ("contact_bitmap", "contact", "Kontakte"))
        for region_name, obj_key, titel in paare:
            region = lay.regions.get(region_name)
            obj = lay.objects.get(obj_key)
            if not region or not obj:
                continue
            bits = radio.read_region(region.addr, region.size)
            n = len(bitmap_indices(bits, obj.count, invert=region.invert))
            print("%-12s: %5d" % (titel, n))
        obj = lay.objects.get("contact")
        if obj:
            erster = radio.read_region(obj.address(0), 32)
            name = erster[1:17].split(b"\x00")[0].decode("latin-1", "replace")
            print("erster Kontakt: %r" % name)
            if name == "Contact1":
                print("  ACHTUNG: Das ist der Werkseintrag - die Kontaktliste wurde "
                      "vom Geraet zurueckgesetzt.")
    finally:
        radio.close()
    return 0


def cmd_read(args) -> int:
    radio = _connect(args)
    try:
        lay = layout_mod.load(args.layout) if args.layout else layout_mod.load_for(radio.info.model)
        img = read_image(radio, lay, smart=not args.full,
                         progress=_progress_printer(), log=_log)
    finally:
        radio.close()
    img.save(args.out)
    print("Gesichert: %s (%d Byte in %d Bereichen)" % (args.out, img.size(), len(img.ranges())))
    checks = verify_layout(img, lay)
    for c in checks:
        print("  %s" % c.line())
    return 0


def cmd_write(args) -> int:
    img = MemoryImage.load(args.image)
    print("Abbild %s: %d Byte, erstellt %s, Modell %s"
          % (args.image, img.size(), img.meta.get("created", "?"), img.meta.get("model", "?")))
    img = rebuild_contact_index(img, log=_log)
    radio = _connect(args)
    try:
        if img.meta.get("model") and radio.info.model and \
                img.meta["model"] != radio.info.model:
            print("WARNUNG: Abbild stammt von %s, angeschlossen ist %s"
                  % (img.meta["model"], radio.info.model))
            if not _confirm("Trotzdem schreiben?", args.yes):
                return 2
        before = None
        if args.backup:
            _log("Sicherheitskopie wird gelesen ...")
            before = read_image(radio, layout_mod.load_for(radio.info.model),
                                smart=False, progress=_progress_printer(), log=_log)
            before.save(args.backup)
            print("Sicherheitskopie: %s" % args.backup)
        if not _confirm("Codeplug jetzt ins Funkgeraet schreiben?", args.yes):
            return 2
        # Die eben gelesene Sicherheitskopie ist zugleich der Vergleichsstand -
        # ein zweites Lesen waere verschenkte Zeit.
        report = write_image(radio, img, differential=not args.no_diff,
                             verify=not args.no_verify, progress=_progress_printer(),
                             log=_log, include_protected=args.include_contacts,
                             current=before)
    finally:
        radio.close()
    print(report.summary())
    if report.mismatches:
        print("FEHLER: Abweichungen bei %d Bloecken, z.B. 0x%08X"
              % (len(report.mismatches), report.mismatches[0]))
        return 1
    return 0


def cmd_verify(args) -> int:
    lay = layout_mod.load(args.layout) if args.layout else layout_mod.load()
    img = MemoryImage.load(args.image)
    checks = verify_layout(img, lay)
    ok = True
    for c in checks:
        print(c.line())
        if c.records and c.failures:
            ok = False
            print("    Abweichende Datensaetze (erste 10): %s"
                  % ", ".join(str(i) for i in c.failures[:10]))
    print()
    print("Ergebnis: %s" % ("Layout passt zu diesem Abbild."
                            if ok else "Layout passt NICHT vollstaendig - "
                                       "Feldschreiben bleibt gesperrt."))
    return 0 if ok else 1


def cmd_decode(args) -> int:
    lay = layout_mod.load(args.layout) if args.layout else layout_mod.load()
    img = MemoryImage.load(args.image)
    cp = decode(img, lay, log=_log)
    if args.project:
        cp.save(args.project)
        print("Projekt geschrieben: %s" % args.project)
    if args.csv_dir:
        from .csvio import export_all
        files = export_all(args.csv_dir, cp)
        print("CSV geschrieben: %s" % ", ".join(os.path.basename(f) for f in files))
    if not args.project and not args.csv_dir:
        for key, value in cp.stats().items():
            print("%-12s %d" % (key, value))
        for ch in cp.channels[:20]:
            print("  %-16s %s" % (ch.name, ch.rx_freq / 1e6))
    return 0


def cmd_export(args) -> int:
    from .csvio import export_all
    cp = Codeplug.load(args.project)
    files = export_all(args.csv_dir, cp)
    print("%d Dateien nach %s geschrieben:" % (len(files), args.csv_dir))
    for f in files:
        print("  %s" % os.path.basename(f))
    return 0


def cmd_import(args) -> int:
    from .csvio import import_all
    cp, loaded = import_all(args.csv_dir)
    if not loaded:
        print("Keine CPS-CSV-Dateien in %s gefunden." % args.csv_dir)
        return 1
    cp.name = args.name or os.path.basename(os.path.abspath(args.csv_dir))
    cp.save(args.project)
    print("Gelesen: %s" % ", ".join(loaded))
    print("Projekt: %s (%s)" % (args.project,
                                ", ".join("%s=%d" % kv for kv in cp.stats().items())))
    return _report_issues(cp, args.strict)


def cmd_validate(args) -> int:
    cp = Codeplug.load(args.project)
    print("Projekt %s: %s" % (args.project, ", ".join("%s=%d" % kv for kv in cp.stats().items())))
    return _report_issues(cp, args.strict)


def _report_issues(cp: Codeplug, strict: bool = False) -> int:
    issues = cp.validate()
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    for issue in issues[:200]:
        print("  %s" % issue)
    if len(issues) > 200:
        print("  ... %d weitere Meldungen" % (len(issues) - 200))
    print("%d Fehler, %d Warnungen" % (len(errors), len(warnings)))
    if errors:
        return 1
    return 1 if (strict and warnings) else 0


def cmd_apply(args) -> int:
    """Geaenderte Kanaele eines Projekts in ein Abbild uebernehmen."""
    lay = layout_mod.load(args.layout) if args.layout else layout_mod.load()
    img = MemoryImage.load(args.image)
    cp = Codeplug.load(args.project)
    allowed, reason = field_write_allowed(img, lay)
    print(reason)
    if not allowed and not args.force:
        print("Abbruch: feldweises Schreiben ist gesperrt (--force ueberstimmt das).")
        return 1
    known = [c for c in cp.channels if (c.extra or {}).get("_index") is not None]
    if not known:
        print("Keine Kanaele mit bekanntem Geraeteindex. Das Projekt muss aus einem "
              "Abbild stammen ('decode'), damit die Zuordnung eindeutig ist.")
        return 1
    issues = [i for i in cp.validate() if i.level == "error"]
    if issues and not args.force:
        for issue in issues[:20]:
            print("  %s" % issue)
        print("Abbruch: %d Fehler im Projekt (--force ueberstimmt das)." % len(issues))
        return 1
    out = patch_settings(patch_channels(img, cp, lay, log=_log), cp, lay, log=_log)
    changed = img.diff(out)
    out.meta["patched_from"] = args.project
    out.save(args.out)
    print("%d geaenderte Stellen, geschrieben nach %s" % (len(changed), args.out))
    print("Naechster Schritt:  atprog write %s" % args.out)
    return 0


def cmd_diff(args) -> int:
    a, b = MemoryImage.load(args.old), MemoryImage.load(args.new)
    runs = a.diff(b)
    if not runs:
        print("Keine Unterschiede.")
        return 0
    lay = layout_mod.load(args.layout) if args.layout else layout_mod.load()
    print("%d Unterschiede:" % len(runs))
    for addr, old, new in runs[:args.limit]:
        where = _locate(lay, addr)
        print("0x%08X %-28s" % (addr, where))
        print("   alt: %s" % old.hex(" "))
        print("   neu: %s" % new.hex(" "))
    if len(runs) > args.limit:
        print("... %d weitere" % (len(runs) - args.limit))
    return 0


def _locate(lay, addr: int) -> str:
    for name, region in lay.regions.items():
        if region.addr <= addr < region.end:
            return "%s +0x%X" % (name, addr - region.addr)
    for key, obj in lay.objects.items():
        for idx in range(obj.count):
            base = obj.address(idx)
            if base <= addr < base + obj.record_size:
                off = addr - base
                field = next((f.name for f in obj.fields
                              if f.offset <= off < f.offset + max(1, f.length or 1)), "?")
                return "%s[%d] +0x%X (%s)" % (key, idx, off, field)
    return "unbekannt"


def cmd_explore(args) -> int:
    """Sucht im Geraetespeicher nach belegten Bereichen.

    Liest in groben Schritten Stichproben und meldet, wo Daten stehen. So
    lassen sich noch unbekannte Ablagen (z.B. RX-Gruppenlisten) finden, ohne
    den gesamten Adressraum zu uebertragen.
    """
    start, stop, step = int(args.start, 0), int(args.stop, 0), int(args.step, 0)
    radio = _connect(args)
    hits, probes = [], 0
    try:
        addr = start
        total = max(1, (stop - start) // step)
        while addr < stop:
            data = radio.read_block(addr, 16)
            probes += 1
            if set(data) - {0x00, 0xFF}:
                text = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
                hits.append((addr, data, text))
                print("0x%08X  %s  |%s|" % (addr, data.hex(" "), text))
            if probes % 25 == 0:
                sys.stderr.write("\r  %d/%d Stichproben, %d Treffer" % (probes, total, len(hits)))
                sys.stderr.flush()
            addr += step
    finally:
        sys.stderr.write("\n")
        radio.close()
    print()
    print("%d Stichproben, %d belegte Stellen" % (probes, len(hits)))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            for addr, data, text in hits:
                fh.write("0x%08X  %s  |%s|\n" % (addr, data.hex(" "), text))
        print("Gespeichert: %s" % args.out)
    return 0


def _beschreibe(data: bytes, addr: int) -> str:
    """Kurzbeschreibung eines Speicherinhalts in Worten.

    Hexdumps werden von manchen Anzeigen als moegliches Schluesselmaterial
    unkenntlich gemacht; diese Zusammenfassung kommt immer durch.
    """
    n = len(data)
    ff = data.count(0xFF)
    null = data.count(0x00)
    druckbar = sum(1 for b in data if 32 <= b < 127)
    verschieden = len(set(data))
    teile = ["%d Byte ab 0x%08X" % (n, addr)]
    if ff == n:
        teile.append("vollstaendig leer (nur 0xFF)")
    elif null == n:
        teile.append("vollstaendig 0x00")
    else:
        teile.append("%d%% 0xFF, %d%% 0x00, %d verschiedene Bytewerte"
                     % (100 * ff // n, 100 * null // n, verschieden))
        if druckbar > n // 2:
            teile.append("ueberwiegend Text")
    # Erkennt das Muster aus 'writelab'?
    passt = sum(1 for i in range(0, min(n, 4096))
                if data[i] == ((i // 256) & 0xFF if i % 256 == 0 else ((i * 7 + 13) & 0xFF)))
    if passt > min(n, 4096) * 0.9:
        teile.append("entspricht dem Testmuster aus 'writelab'")
    return "Inhalt: " + ", ".join(teile)


def cmd_peek(args) -> int:
    """Liest einen beliebigen Speicherbereich aus dem Geraet."""
    addr, size = int(args.addr, 0), int(args.size, 0)
    radio = _connect(args)
    try:
        data = radio.read_region(addr, size, progress=_progress_printer(),
                                 label="Lese 0x%08X" % addr)
    finally:
        radio.close()
    img = MemoryImage({"model": radio.info.model if radio.info else "",
                       "firmware": radio.info.version if radio.info else "",
                       "purpose": "peek", "addr": "0x%08X" % addr})
    img.write(addr, data)
    print(_beschreibe(data, addr))
    if args.out:
        img.save(args.out)
        print("Gespeichert: %s (%d Byte ab 0x%08X)" % (args.out, size, addr))
    if args.print or not args.out:
        print(img.hexdump(addr, min(size, args.lines * 16)))
    return 0


def cmd_merge(args) -> int:
    """Fuegt mehrere Abbilder zusammen (spaetere ueberschreiben fruehere)."""
    merged = MemoryImage.load(args.images[0])
    for path in args.images[1:]:
        other = MemoryImage.load(path)
        for addr, size in other.ranges():
            merged.write(addr, other.read(addr, size))
        print("uebernommen: %s (%d Byte)" % (path, other.size()))
    merged.meta["merged_from"] = [os.path.basename(p) for p in args.images]
    merged.save(args.out)
    print("Geschrieben: %s (%d Byte in %d Bereichen)"
          % (args.out, merged.size(), len(merged.ranges())))
    return 0


# --- Rufzeichendatenbank ---------------------------------------------------

def _userdb_from_source(args) -> "object":
    """Erzeugt eine Datenbank aus einer CSV- oder JSON-Liste."""
    from .userdb import UserDatabase, read_user_file
    prefixes = [p.strip() for p in (args.prefix or "").split(",") if p.strip()]
    entries = read_user_file(args.csv, country=args.country or "",
                             prefixes=prefixes, limit=args.limit)
    if not entries:
        raise SystemExit("Keine passenden Eintraege in %s gefunden." % args.csv)
    print("%d Eintraege ausgewaehlt%s%s" % (
        len(entries),
        " (Land %s)" % args.country if args.country else "",
        " (Praefix %s)" % ",".join(prefixes) if prefixes else ""))
    return UserDatabase(entries=entries)


def _userdb_connect(args):
    """Verbindet und ermittelt Geometrie, Eintragszahl und ID-Kodierung."""
    from .userdb import probe
    radio = _connect(args)
    status = probe(radio, getattr(args, "geometry", "") or "", log=_log)
    return radio, status


def cmd_userdb_info(args) -> int:
    """Kopf und Stichproben der Datenbank im Geraet anzeigen."""
    from .userdb import sample_entries
    radio, status = _userdb_connect(args)
    try:
        geo = status.geometry
        print("Geometrie             : %s (Index 0x%08X, Daten 0x%08X, Kopf 0x%08X)"
              % (geo.name, geo.index_base, geo.entry_base, geo.limits))
        print("Eintraege             : %d" % status.count)
        if not status.count:
            print("Es ist keine Rufzeichendatenbank geladen.")
            return 0
        print("ID-Kodierung          : %s"
              % ("BCD" if status.bcd_id else
                 "binaer" if status.bcd_id is False else "unbekannt"))
        if status.bcd_id is None:
            print("Die Datensaetze liessen sich nicht zuordnen.")
            return 1
        print("\nStichproben ueber die Datenbank:")
        for entry in sample_entries(radio, status, args.samples):
            print("  %8d  %-10s %-16s %-15s %s"
                  % (entry.id, entry.callsign, entry.name, entry.city, entry.country))
    finally:
        radio.close()
    return 0


def cmd_userdb_export(args) -> int:
    """Datenbank aus dem Geraet lesen und als CSV sichern."""
    from .userdb import read_from_radio, write_user_csv
    radio, status = _userdb_connect(args)
    try:
        if not status.readable:
            print("Es ist keine lesbare Rufzeichendatenbank geladen.")
            return 1
        wanted = min(status.count, args.limit) if args.limit else status.count
        print("Lese %d von %d Eintraegen." % (wanted, status.count))
        entries = read_from_radio(radio, status, limit=args.limit,
                                  progress=_progress_printer())
    finally:
        radio.close()
    written = write_user_csv(args.out, entries)
    print("%d Eintraege geschrieben: %s" % (written, args.out))
    return 0


def cmd_userdb_verify(args) -> int:
    """Prueft die Datenbank im Geraet auf Vollstaendigkeit."""
    from .userdb import verify_on_radio
    radio, status = _userdb_connect(args)
    try:
        print("Eintraege laut Geraet: %d" % status.count)
        if not status.readable:
            print("Keine lesbare Datenbank geladen.")
            return 1
        result = verify_on_radio(radio, status, samples=args.samples,
                                 progress=_progress_printer())
    finally:
        radio.close()
    print("%d von %d Stichproben in Ordnung." % (result["ok"], result["checked"]))
    if result["bad"]:
        grenze = result.get("boundary") or result["first_bad"]
        fehlend = status.count - grenze
        print("Erster fehlerhafter Eintrag: %d von %d (%.1f%% der Liste)."
              % (grenze, status.count, 100.0 * grenze / max(1, status.count)))
        print("Betroffen sind damit rund %d Eintraege am Ende." % fehlend)
        print("Fehlerhafte Stichproben: %s"
              % ", ".join(str(i) for i in result["bad"][:12]))
        if fehlend <= 5:
            print("\nNur die allerletzten Eintraege - fuer den Betrieb ohne Bedeutung.")
        else:
            print("\nDas deutet auf einen abgebrochenen Schreibvorgang hin.")
            print("Abhilfe: die Liste erneut schreiben (atprog userdb write ...).")
        return 1
    print("Die Datenbank ist durchgaengig lesbar.")
    return 0


def cmd_userdb_fetch(args) -> int:
    """Laedt die aktuelle Nutzerliste von radioid.net."""
    from .userdb import RADIOID_URL, fetch_user_database, read_user_file
    url = args.url or RADIOID_URL
    print("Quelle: %s" % url)
    try:
        size = fetch_user_database(args.out, url=url, progress=_progress_printer())
    except Exception as exc:
        print("Abruf fehlgeschlagen: %s" % exc)
        print("Die Liste laesst sich auch von Hand herunterladen:")
        print("  https://radioid.net/database/dumps  ->  user.json oder user.csv")
        return 1
    print("Geladen: %s (%.1f MB)" % (args.out, size / 1048576.0))
    if args.count:
        entries = read_user_file(args.out)
        print("Enthalten: %d Eintraege" % len(entries))
        laender = {}
        for e in entries:
            laender[e.country or "?"] = laender.get(e.country or "?", 0) + 1
        top = sorted(laender.items(), key=lambda kv: -kv[1])[:5]
        print("Groesste Laender: %s" % ", ".join("%s %d" % kv for kv in top))
    return 0


def cmd_userdb_build(args) -> int:
    db = _userdb_from_source(args)
    img = db.build(progress=_progress_printer())
    img.save(args.out)
    print("Abbild: %s (%d Byte in %d Bereichen)" % (args.out, img.size(), len(img.ranges())))
    print("Schreiben mit:  atprog userdb write --image %s" % args.out)
    return 0


def cmd_userdb_write(args) -> int:
    """Rufzeichendatenbank ins Geraet uebertragen."""
    from .userdb import read_limits, write_to_radio
    if args.image:
        img, db = MemoryImage.load(args.image), None
    else:
        if not args.csv:
            raise SystemExit("Entweder eine CSV angeben oder --image.")
        img, db = None, _userdb_from_source(args)

    radio, status = _userdb_connect(args)
    try:
        if db is not None:
            db.geometry = status.geometry
            img = db.build(progress=_progress_printer())
        new_count, _end = read_limits(img, status.geometry)
        print("Im Geraet bisher  : %d Eintraege" % status.count)
        print("Neu zu schreiben  : %d Eintraege, %.1f MB"
              % (new_count, img.size() / 1048576.0))
        stale = max(0, status.count - new_count)
        if stale:
            print("Danach zu loeschen : %d ueberzaehlige Indexeintraege" % stale)
        # Geschwindigkeit kurz messen statt schaetzen: ein paar Bloecke lesen
        # und daraus die Kommandorate bestimmen.
        rate = _command_rate(radio)
        commands = (img.size() + stale * 8) / float(radio.write_block_size)
        minutes = commands / rate / 60.0
        print("Gemessen: %.0f Kommandos je Sekunde -> Dauer etwa %s."
              % (rate, "%.0f Minuten" % minutes if minutes < 90
                 else "%.1f Stunden" % (minutes / 60.0)))
        print()
        print("ACHTUNG: Der Bereich ab 0x04000000 enthaelt bei dieser Geraetefamilie")
        print("auch eine Zuordnungstabelle des Codeplugs (beim D868UV 0x04340000).")
        print("Ein Schreibvorgang kann sie ueberschreiben - dann verwirft das Geraet")
        print("die Kontaktliste. Sichern Sie vorher den Codeplug.")
        if not _confirm("Jetzt schreiben?", args.yes):
            return 2
        report = write_to_radio(radio, img, status, progress=_progress_printer(),
                                log=_log)
    finally:
        radio.close()
    if not report["header_ok"]:
        print("FEHLER: Der Kopfblock stimmt nicht mit dem Geschriebenen ueberein.")
        return 1
    for entry in report["samples"]:
        print("  %8d  %-10s %s" % (entry.id, entry.callsign, entry.name))
    if not report["verified"]:
        print("WARNUNG: Keine Stichprobe liess sich zurueckelesen.")
        return 1
    print("Fertig: %d Eintraege uebertragen, %d Stichproben geprueft."
          % (report["count"], report["verified"]))
    return 0


def _command_rate(radio, samples: int = 40) -> float:
    """Misst, wie viele Kommandos je Sekunde die Verbindung schafft."""
    start = time.monotonic()
    for i in range(samples):
        radio.read_block(0x02500000 + i * 16, 16)
    elapsed = max(1e-3, time.monotonic() - start)
    return samples / elapsed


def cmd_writetest(args) -> int:
    """Prueft gefahrlos, ob und wie das Geraet Schreibzugriffe annimmt.

    Es wird ein Block gelesen und *unveraendert* zurueckgeschrieben - der
    Inhalt des Geraets bleibt damit derselbe, egal wie der Test ausgeht.
    """
    addr = int(args.addr, 0)
    radio = _connect(args)
    results = []
    try:
        for size in (16, 32, 64):
            original = radio.read_region(addr, size)
            try:
                radio.write_block(addr, original)
                back = radio.read_region(addr, size)
                ok = back == original
            except Exception as exc:
                ok, back = False, None
                _log("%d Byte: %s" % (size, exc))
            results.append((size, ok))
            print("  %2d Byte je Kommando: %s" % (size, "angenommen" if ok else "abgelehnt"))
    finally:
        radio.close()
    good = [size for size, ok in results if ok]
    print()
    if not good:
        print("Das Geraet nimmt an dieser Adresse keine Schreibzugriffe an.")
        return 1
    print("Groesste angenommene Schreibblockgroesse: %d Byte" % max(good))
    print("Verwenden mit:  --write-block %d" % max(good))
    return 0


ZEICHEN = {"ausgewertet": "#", "gesichert": "+", "teils": "~", "unbekannt": "."}


def cmd_map(args) -> int:
    """Zeigt den Adressplan als Uebersicht: was ist zugeordnet, was nicht."""
    lay = layout_mod.load(args.name) if args.name else layout_mod.load()
    print("%s" % lay.title)
    print("Adressplan %s, Revision %d vom %s" % (lay.id, lay.revision, lay.revised))
    if lay.verified_on:
        print("Geprueft an: %s" % lay.verified_on)
    print()
    print("  # ausgewertet   + gesichert, Inhalt nicht gedeutet   ~ teils   . unbekannt")
    print()

    breite = 44
    print("Der Adressraum im Ueberblick")
    print("  %-44s %-11s %9s  %s" % ("Bereich", "Adresse", "Groesse", ""))
    for b in lay.bereiche_gross:
        addr, size = int(b["addr"], 0), int(b["size"], 0)
        art = b.get("einstufung", "unbekannt")
        balken = ZEICHEN.get(art, ".") * max(1, min(breite, size * breite // 0x2A60000))
        print("  %-44s 0x%08X %8.1f MB  %s"
              % (b["name"][:44], addr, size / 1048576.0, balken))
    print()

    print("Einzelbereiche des Codeplugs")
    for name, r in sorted(lay.regions.items(), key=lambda kv: kv[1].addr):
        print("  %s 0x%08X %7d B  %-16s %s"
              % (ZEICHEN.get(r.einstufung, "."), r.addr, r.size, name, r.zweck or r.desc))
    print()

    print("Datensaetze (aus dem Abbild decodiert)")
    for key, o in sorted(lay.objects.items(), key=lambda kv: kv[1].base):
        felder = ", ".join(f.name for f in o.fields[:6])
        if len(o.fields) > 6:
            felder += " ..."
        print("  # 0x%08X %5d x %4d B  %-14s %s" % (o.base, o.count, o.record_size, key, felder))
    print()

    if lay.unzugeordnet:
        print("Noch nicht zugeordnet")
        for u in lay.unzugeordnet:
            print("  . 0x%08X %7d B  %s" % (int(u["addr"], 0), int(u["size"], 0), u["befund"]))
        print()

    if args.verlauf and lay.revisionen:
        print("Wie der Adressplan entstanden ist")
        for r in lay.revisionen:
            print("  r%-3d %s  %s" % (r["rev"], r["datum"], r["aenderung"]))
        print()

    zugeordnet = sum(o.count * o.record_size for o in lay.objects.values())
    gesichert = sum(r.size for r in lay.regions.values())
    offen = sum(int(u["size"], 0) for u in lay.unzugeordnet)
    print("Zusammen: %.1f MB in Datensaetzen ausgewertet, %.0f KB als Bereiche gesichert, "
          "%.0f KB offen." % (zugeordnet / 1048576.0, gesichert / 1024.0, offen / 1024.0))
    return 0


def cmd_layout(args) -> int:
    if args.list:
        for name in layout_mod.available():
            lay = layout_mod.load(name)
            print("%-16s %s (Revision %d)" % (name, lay.title, lay.revision))
        return 0
    lay = layout_mod.load(args.name)
    print(lay.describe())
    if lay.notes:
        print("\nHinweise:")
        for note in lay.notes:
            print("  %s" % note)
    return 0


def cmd_dump(args) -> int:
    img = MemoryImage.load(args.image)
    print(img.hexdump(int(args.addr, 0), args.size))
    return 0


def cmd_gui(args) -> int:
    from .webui import serve
    serve(host=args.host, port=args.port, open_browser=not args.no_browser,
          workdir=args.workdir)
    return 0


def cmd_simulate(args) -> int:
    from .simulator import SimulatedRadio, populate_demo
    sim = SimulatedRadio()
    port = sim.start()
    populate_demo(sim, channels=args.channels)
    print("Simuliertes Funkgeraet laeuft auf %s" % port)
    print("Beispiel:  atprog read --port %s --out demo.atbin" % port)
    print("Beenden mit Strg-C./control-c")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


# ---------------------------------------------------------------------------
# Argumentbaum
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="atprog", description="%s %s" % (APP_TITLE, __version__),
        epilog="Ohne Unterbefehl startet die grafische Oberflaeche (atprog gui).")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = p.add_subparsers(dest="command")

    def add_port_args(sp):
        sp.add_argument("-p", "--port", help="Serieller Port (Vorgabe: automatisch)")
        sp.add_argument("--block", type=int, default=64,
                        help="Bytes je Lesekommando, 1..64 (Vorgabe 64)")
        sp.add_argument("--write-block", type=int, default=16,
                        help="Bytes je Schreibkommando, 1..64 (Vorgabe 16)")
        sp.add_argument("--timeout", type=float, default=2.0, help="Timeout je Kommando")

    sp = sub.add_parser("ports", help="Verfuegbare serielle Ports anzeigen")
    sp.set_defaults(func=cmd_ports)

    sp = sub.add_parser("info", help="Geraet identifizieren")
    add_port_args(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("writelab", help="Schreibverhalten in leerem Block vermessen")
    add_port_args(sp)
    sp.add_argument("--addr", default="0x02940000", help="Versuchsbereich")
    sp.add_argument("--offset", type=lambda v: int(v, 0), default=0x10000)
    sp.add_argument("--only", help="Nur diese Adresse pruefen, z.B. 0x02540000")
    sp.set_defaults(func=cmd_writelab)

    sp = sub.add_parser("snapshot", help="Diagnosebereiche am Stueck lesen (fuer diff)")
    add_port_args(sp)
    sp.add_argument("-o", "--out", default="schnappschuss.atbin")
    sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser("zones", help="Zonen auflisten, sichtbar oder versteckt schalten")
    add_port_args(sp)
    sp.add_argument("--show-all", action="store_true", help="Alle Zonen sichtbar schalten")
    sp.add_argument("--hide-all", action="store_true", help="Alle Zonen verstecken")
    sp.set_defaults(func=cmd_zones)

    sp = sub.add_parser("status", help="Schnell nachsehen, was im Geraet belegt ist")
    add_port_args(sp)
    sp.add_argument("--layout")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("read", help="Codeplug aus dem Geraet sichern")
    add_port_args(sp)
    sp.add_argument("-o", "--out", default="codeplug.atbin", help="Zieldatei")
    sp.add_argument("--full", action="store_true", help="Alle Bereiche lesen (langsam)")
    sp.add_argument("--layout", help="Layoutname erzwingen")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("write", help="Gesichertes Abbild ins Geraet schreiben")
    add_port_args(sp)
    sp.add_argument("image", help="Abbilddatei (.atbin)")
    sp.add_argument("--backup", help="Vorher Sicherheitskopie in diese Datei lesen")
    sp.add_argument("--no-diff", action="store_true", help="Alle Bloecke schreiben")
    sp.add_argument("--no-verify", action="store_true", help="Nicht zurueckpruefen")
    sp.add_argument("--full", action="store_true", help="Sicherheitskopie vollstaendig lesen")
    sp.add_argument("-y", "--yes", action="store_true", help="Ohne Rueckfrage schreiben")
    sp.add_argument("--include-contacts", action="store_true",
                    help="Auch den Kontaktbereich schreiben (siehe Warnung in der Doku)")
    sp.set_defaults(func=cmd_write)

    sp = sub.add_parser("verify", help="Layout gegen ein Abbild pruefen (Round-Trip)")
    sp.add_argument("image")
    sp.add_argument("--layout")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("decode", help="Abbild in Projekt/CSV wandeln")
    sp.add_argument("image")
    sp.add_argument("--project", help="Projektdatei (.json) schreiben")
    sp.add_argument("--csv-dir", help="CPS-CSV-Dateien in dieses Verzeichnis schreiben")
    sp.add_argument("--layout")
    sp.set_defaults(func=cmd_decode)

    sp = sub.add_parser("export", help="Projekt als CPS-CSV exportieren")
    sp.add_argument("project")
    sp.add_argument("csv_dir")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("import", help="CPS-CSV-Verzeichnis als Projekt einlesen")
    sp.add_argument("csv_dir")
    sp.add_argument("project")
    sp.add_argument("--name", help="Projektname")
    sp.add_argument("--strict", action="store_true", help="Warnungen wie Fehler behandeln")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("validate", help="Projekt auf Geraetetauglichkeit pruefen")
    sp.add_argument("project")
    sp.add_argument("--strict", action="store_true")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("apply", help="Projektaenderungen in ein Abbild uebernehmen")
    sp.add_argument("project")
    sp.add_argument("image")
    sp.add_argument("-o", "--out", default="geaendert.atbin", help="Zielabbild")
    sp.add_argument("--layout")
    sp.add_argument("--force", action="store_true",
                    help="Auch bei fehlgeschlagener Layoutpruefung fortfahren")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("diff", help="Zwei Abbilder vergleichen (Layout-Kalibrierung)")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--layout")
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("dump", help="Hexdump eines Abbildbereichs")
    sp.add_argument("image")
    sp.add_argument("addr", help="Adresse, z.B. 0x00800000")
    sp.add_argument("--size", type=int, default=256)
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("map", help="Adressplan als Uebersicht anzeigen")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--verlauf", action="store_true",
                    help="Auch zeigen, wie der Adressplan entstanden ist")
    sp.set_defaults(func=cmd_map)

    sp = sub.add_parser("layout", help="Speicherlayout anzeigen")
    sp.add_argument("name", nargs="?", default=layout_mod.DEFAULT_LAYOUT)
    sp.add_argument("--list", action="store_true")
    sp.set_defaults(func=cmd_layout)

    sp = sub.add_parser("userdb", help="Rufzeichendatenbank (DMR-ID-Liste) verwalten")
    usub = sp.add_subparsers(dest="userdb_command", required=True)

    isp = usub.add_parser("info", help="Kopf und erste Eintraege im Geraet anzeigen")
    add_port_args(isp)
    isp.add_argument("--samples", type=int, default=8, help="Anzahl Stichproben")
    isp.add_argument("--geometry", help="Ablage erzwingen: d878uv2 oder d868uv")
    isp.set_defaults(func=cmd_userdb_info)

    esp = usub.add_parser("export", help="Datenbank aus dem Geraet als CSV sichern")
    add_port_args(esp)
    esp.add_argument("-o", "--out", default="userdb.csv")
    esp.add_argument("--limit", type=int, default=0, help="Nur die ersten N Eintraege")
    esp.add_argument("--geometry", help="Ablage erzwingen: d878uv2 oder d868uv")
    esp.set_defaults(func=cmd_userdb_export)

    vsp = usub.add_parser("verify", help="Datenbank im Geraet auf Vollstaendigkeit pruefen")
    add_port_args(vsp)
    vsp.add_argument("--samples", type=int, default=40, help="Anzahl Stichproben")
    vsp.add_argument("--geometry")
    vsp.set_defaults(func=cmd_userdb_verify)

    fsp = usub.add_parser("fetch", help="Aktuelle Nutzerliste von radioid.net laden")
    fsp.add_argument("-o", "--out", default="radioid-users.json", help="Zieldatei")
    fsp.add_argument("--url", help="Abweichende Quelle")
    fsp.add_argument("--count", action="store_true", help="Danach Eintraege zaehlen")
    fsp.set_defaults(func=cmd_userdb_fetch)

    bsp = usub.add_parser("build", help="Datenbank aus radioid.net-Liste erzeugen")
    bsp.add_argument("csv", help="CSV oder JSON von radioid.net")
    bsp.add_argument("-o", "--out", default="userdb.atbin")
    bsp.add_argument("--country", help="Nur dieses Land, z.B. Germany")
    bsp.add_argument("--prefix", help="Nur diese ID-Praefixe, z.B. 262,263")
    bsp.add_argument("--limit", type=int, default=0)
    bsp.set_defaults(func=cmd_userdb_build)

    wsp = usub.add_parser("write", help="Datenbank ins Geraet schreiben")
    add_port_args(wsp)
    wsp.add_argument("csv", nargs="?", help="CSV oder JSON von radioid.net")
    wsp.add_argument("--image", help="Fertiges Abbild aus 'userdb build'")
    wsp.add_argument("--country")
    wsp.add_argument("--prefix")
    wsp.add_argument("--limit", type=int, default=0)
    wsp.add_argument("-y", "--yes", action="store_true")
    wsp.add_argument("--geometry", help="Ablage erzwingen: d878uv2 oder d868uv")
    wsp.set_defaults(func=cmd_userdb_write)

    sp = sub.add_parser("writetest", help="Gefahrlos pruefen, ob Schreiben funktioniert")
    add_port_args(sp)
    sp.add_argument("--addr", default="0x02500000",
                    help="Zu pruefende Adresse (Vorgabe: Einstellungsbereich)")
    sp.set_defaults(func=cmd_writetest)

    sp = sub.add_parser("merge", help="Abbilder zusammenfuehren (z.B. Sicherung + peek)")
    sp.add_argument("images", nargs="+", help="Quellabbilder in Reihenfolge")
    sp.add_argument("-o", "--out", required=True, help="Zielabbild")
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser("peek", help="Beliebigen Speicherbereich auslesen")
    add_port_args(sp)
    sp.add_argument("addr", help="Startadresse, z.B. 0x02980000")
    sp.add_argument("--size", default="0x1000", help="Laenge in Byte (Vorgabe 0x1000)")
    sp.add_argument("-o", "--out", help="Als Abbilddatei speichern")
    sp.add_argument("--print", action="store_true", help="Auch bei --out anzeigen")
    sp.add_argument("--lines", type=int, default=32, help="Anzuzeigende Hexdump-Zeilen")
    sp.set_defaults(func=cmd_peek)

    sp = sub.add_parser("explore", help="Geraetespeicher nach belegten Bereichen absuchen")
    add_port_args(sp)
    sp.add_argument("--start", default="0x02400000", help="Startadresse")
    sp.add_argument("--stop", default="0x02800000", help="Endadresse")
    sp.add_argument("--step", default="0x1000", help="Schrittweite der Stichproben")
    sp.add_argument("-o", "--out", help="Treffer zusaetzlich in diese Datei schreiben")
    sp.set_defaults(func=cmd_explore)

    sp = sub.add_parser("gui", help="Grafische Oberflaeche im Browser starten")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("--workdir", default=os.getcwd())
    sp.set_defaults(func=cmd_gui)

    sp = sub.add_parser("simulate", help="Virtuelles Funkgeraet zum Ausprobieren starten")
    sp.add_argument("--channels", type=int, default=8)
    sp.set_defaults(func=cmd_simulate)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["gui"])      # ohne Unterbefehl: Oberflaeche
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130
    except Exception as exc:
        print("Fehler: %s" % exc, file=sys.stderr)
        if os.environ.get("ATPROG_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
