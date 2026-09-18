"""Hochsprachige Geraetefunktionen: Sichern, Ruecksichern, Decodieren.

Sicherheitsgrundsaetze dieses Moduls:
  * Vor jedem Schreiben wird ein vollstaendiges Backup gelesen.
  * Geschrieben wird nur differenziell: unveraenderte Bloecke werden nie
    angefasst.
  * Nach dem Schreiben wird zurueckgelesen und byteweise verglichen.
  * Feldbasiertes Schreiben verlangt eine bestandene Layout-Verifikation.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import layout as layout_mod
from .codec import (bitmap_get, bitmap_indices, bitmap_set, decode_record, encode_record,
                    roundtrip_ok)
from .image import MemoryImage
from .layout import Layout, ObjectDef
from .models import (Channel, Codeplug, RadioID, RxGroupList, ScanList, TalkGroup, Zone,
                     fmt_freq, tone_from_index, tone_to_index)
from .protocol import ProgressFn, Radio

GAP_MERGE = 256          # Luecken bis zu dieser Groesse mitlesen statt neu zu adressieren


# ---------------------------------------------------------------------------
# Hilfsmittel
# ---------------------------------------------------------------------------

@dataclass
class Progress:
    """Fortschrittszaehler ueber mehrere Bereiche hinweg."""
    total: int = 0
    done: int = 0
    callback: Optional[ProgressFn] = None

    def advance(self, n: int, label: str = "") -> None:
        self.done += n
        if self.callback:
            self.callback(self.done, max(self.total, self.done), label)

    def region_progress(self, label: str) -> ProgressFn:
        base = self.done

        def fn(done: int, total: int, _label: str) -> None:
            if self.callback:
                self.callback(base + done, max(self.total, base + done), label)
        return fn


def _merge_runs(addresses: Sequence[Tuple[int, int]], gap: int = GAP_MERGE) -> List[Tuple[int, int]]:
    """Fasst (Adresse, Laenge)-Paare zu moeglichst wenigen Bereichen zusammen."""
    if not addresses:
        return []
    ordered = sorted(addresses)
    runs = [list(ordered[0])]
    for addr, size in ordered[1:]:
        last = runs[-1]
        end = last[0] + last[1]
        if addr <= end + gap:
            last[1] = max(end, addr + size) - last[0]
        else:
            runs.append([addr, size])
    return [(a, s) for a, s in runs]


def record_runs(obj: ObjectDef, indices: Sequence[int]) -> List[Tuple[int, int]]:
    return _merge_runs([(obj.address(i), obj.record_size) for i in indices])


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------

def read_image(radio: Radio, lay: Optional[Layout] = None, smart: bool = True,
               progress: Optional[ProgressFn] = None,
               log: Callable[[str], None] = lambda m: None) -> MemoryImage:
    """Liest den Codeplug aus dem Geraet.

    ``smart=True`` liest zuerst die Belegungsbitmaps und danach nur die
    tatsaechlich benutzten Datensaetze - das verkuerzt den Vorgang von etwa
    1,8 MB auf meist deutlich unter 200 kB.
    """
    lay = lay or layout_mod.load()
    img = MemoryImage({
        "model": radio.info.model if radio.info else "",
        "firmware": radio.info.version if radio.info else "",
        "layout": lay.id, "plan_revision": lay.revision,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "smart": smart,
    })

    control = sorted(lay.regions.values(), key=lambda r: r.addr)
    prog = Progress(total=sum(r.size for r in control), callback=progress)
    for region in control:
        log("Lese %-16s 0x%08X (%d Byte)" % (region.name, region.addr, region.size))
        data = radio.read_region(region.addr, region.size,
                                 progress=prog.region_progress("Bereich %s" % region.name))
        img.write(region.addr, data)
        prog.done += region.size

    for key, obj in lay.objects.items():
        if obj.base == 0 or obj.record_size == 0:
            continue
        if any(r.addr == obj.base for r in control):
            continue        # bereits als Steuerbereich gelesen
        indices = _selected_indices(img, lay, obj, smart)
        if not indices:
            log("%-12s: keine belegten Datensaetze" % key)
            continue
        runs = record_runs(obj, indices)
        total = sum(s for _, s in runs)
        log("%-12s: %d Datensaetze in %d Bloecken (%d Byte)" % (key, len(indices), len(runs), total))
        sub = Progress(total=total, callback=progress)
        for addr, size in runs:
            data = radio.read_region(addr, size, progress=sub.region_progress(obj.title))
            img.write(addr, data)
            sub.done += size
    return img


def _selected_indices(img: MemoryImage, lay: Layout, obj: ObjectDef, smart: bool) -> List[int]:
    if not smart or not obj.bitmap:
        return list(range(obj.count))
    try:
        region = lay.region(obj.bitmap)
        bits = img.read(region.addr, region.size)
    except (KeyError, Exception):
        return list(range(obj.count))
    used = bitmap_indices(bits, obj.count, invert=region.invert)
    return used or list(range(min(obj.count, 128)))


# ---------------------------------------------------------------------------
# Schreiben
# ---------------------------------------------------------------------------

@dataclass
class WriteReport:
    blocks_written: int = 0
    bytes_written: int = 0
    blocks_skipped: int = 0
    blocks_protected: int = 0
    mismatches: List[int] = None

    def __post_init__(self):
        if self.mismatches is None:
            self.mismatches = []

    def summary(self) -> str:
        text = ("%d Bloecke geschrieben (%d Byte), %d unveraendert uebersprungen, "
                "%d Pruefabweichungen" % (self.blocks_written, self.bytes_written,
                                          self.blocks_skipped, len(self.mismatches)))
        if self.blocks_protected:
            text += ", %d im Kontaktbereich ausgespart" % self.blocks_protected
        return text


# Bereiche, die das Geraet selbst verwaltet und die beim Zurueckschreiben
# ausgespart bleiben. Hintergrund: Ein Schreibvorgang ueber den Kontaktbereich
# hat an echter Hardware dazu gefuehrt, dass das Funkgeraet die Kontaktliste
# verworfen und auf den Werkszustand zurueckgesetzt hat. Offenbar genuegt es
# nicht, die Datensaetze zu schreiben - es gehoert eine Struktur dazu, die
# atprog (noch) nicht kennt.
# Frueher gesperrt; seit die Indextabelle mitgeschrieben wird, nicht mehr noetig.
PROTECTED_OBJECTS = ()
PROTECTED_REGIONS = ()

WRITE_LOCK_REASON = """(nicht mehr verwendet)

Frueher gesperrt wegen:

An einem AT-D878UV II Plus (Firmware V101) wurde festgestellt, dass das Geraet
jeden Speicherbereich in ZWEI Kopien im Abstand 0x20000 fuehrt und zwischen
ihnen wechselt; am Ende jedes 0x40000-Blocks stehen Gueltigkeitsmarken
(55 55 AA AA bzw. 22 33 44 55). Welche Kopie gerade gilt, wechselt, sobald das
Geraet selbst schreibt.

atprog adressiert bisher feste Adressen. Trifft das die gerade nicht gueltige
Kopie, nimmt das Geraet die Daten scheinbar an, verwirft sie aber - im
beobachteten Fall hat es daraufhin die gesamte Kontaktliste auf den
Werkszustand zurueckgesetzt.

Lesen ist davon nicht betroffen und bleibt uneingeschraenkt moeglich.
Bis die Kopienverwaltung nachgebildet ist, bleibt Schreiben gesperrt."""


def protected_ranges(lay: Layout) -> List[Tuple[int, int]]:
    """Adressbereiche, die nicht geschrieben werden."""
    out: List[Tuple[int, int]] = []
    for name in PROTECTED_REGIONS:
        region = lay.regions.get(name)
        if region:
            out.append((region.addr, region.size))
    for key in PROTECTED_OBJECTS:
        obj = lay.objects.get(key)
        if obj and obj.count:
            out.append((obj.address(0), obj.count * obj.record_size))
    return out


def write_image(radio: Radio, target: MemoryImage, differential: bool = True,
                verify: bool = True, progress: Optional[ProgressFn] = None,
                log: Callable[[str], None] = lambda m: None,
                current: Optional[MemoryImage] = None,
                lay: Optional[Layout] = None,
                include_protected: bool = False,
                accept_risk: bool = False) -> WriteReport:
    """Schreibt ein Abbild zurueck ins Geraet.

    ``differential=True`` liest zuvor den betroffenen Bereich und uebertraegt
    nur geaenderte Bloecke. Der Kontaktbereich bleibt ausgespart, solange
    ``include_protected`` nicht gesetzt ist - siehe ``PROTECTED_OBJECTS``.
    """
    report = WriteReport()
    block = radio.write_block_size
    jobs: List[Tuple[int, bytes]] = []

    # Das Geraet sammelt Schreibzugriffe und uebernimmt sie erst beim END.
    # Wer in derselben Sitzung liest, bekommt den alten Flash-Inhalt und
    # verwirft dabei den Puffer. Deshalb strikt getrennte Sitzungen:
    #   1. Vergleichsstand lesen   2. nur schreiben   3. nach Neustart pruefen
    if differential:
        log("Sitzung 1 von 3: Vergleichsstand lesen ...")
        if current is not None:
            # Deckt der uebergebene Vergleichsstand das Ziel nicht vollstaendig
            # ab, waere jeder fehlende Block faelschlich "geaendert". Dann lieber
            # neu lesen, als den ganzen Codeplug zu schreiben.
            fehlend = [(a, n) for a, n in target.ranges() if not current.has(a, n)]
            if fehlend:
                log("Vergleichsstand deckt %d Bereiche nicht ab - wird nachgelesen"
                    % len(fehlend))
                current = None
        current = current or _read_same_ranges(radio, target, progress, log)
        changed = current.changed_blocks(target, block=block)
        total_blocks = sum((size + block - 1) // block for _, size in target.ranges())
        report.blocks_skipped = total_blocks - len(changed)
        jobs = changed
    else:
        for addr, size in target.ranges():
            data = target.read(addr, size)
            for off in range(0, size, block):
                jobs.append((addr + off, data[off:off + block]))

    if jobs:
        log("Sitzung 2 von 3: schreiben (ohne Zwischenlesen) ...")
        radio.reopen()

    if not include_protected:
        lay = lay or layout_mod.load()
        tabu = protected_ranges(lay)
        vorher = len(jobs)
        jobs = [(addr, data) for addr, data in jobs
                if not any(start <= addr < start + size for start, size in tabu)]
        if vorher != len(jobs):
            report.blocks_protected = vorher - len(jobs)
            log("%d Bloecke im geschuetzten Kontaktbereich werden nicht geschrieben"
                % report.blocks_protected)

    prog = Progress(total=sum(len(d) for d in (j[1] for j in jobs)), callback=progress)
    log("%d Bloecke zu schreiben (%d Byte)" % (len(jobs), prog.total))
    for addr, data in jobs:
        radio.write_block(addr, data)
        report.blocks_written += 1
        report.bytes_written += len(data)
        prog.advance(len(data), "Schreibe 0x%08X" % addr)

    if verify and jobs:
        log("Sitzung 3 von 3: Neustart abwarten und pruefen ...")
        radio.reopen()
        vprog = Progress(total=sum(len(d) for _, d in jobs), callback=progress)
        for addr, data in jobs:
            actual = radio.read_block(addr, len(data))
            if actual != data:
                report.mismatches.append(addr)
            vprog.advance(len(data), "Pruefe 0x%08X" % addr)
    return report


def _read_same_ranges(radio: Radio, target: MemoryImage,
                      progress: Optional[ProgressFn],
                      log: Callable[[str], None]) -> MemoryImage:
    current = MemoryImage({"purpose": "pre-write-backup"})
    prog = Progress(total=sum(s for _, s in target.ranges()), callback=progress)
    for addr, size in target.ranges():
        data = radio.read_region(addr, size, progress=prog.region_progress("Vergleichslesen"))
        current.write(addr, data)
    return current


# ---------------------------------------------------------------------------
# Layout-Verifikation
# ---------------------------------------------------------------------------

@dataclass
class LayoutCheck:
    object_key: str
    title: str
    records: int = 0
    roundtrip_ok: int = 0
    failures: List[int] = None

    def __post_init__(self):
        if self.failures is None:
            self.failures = []

    @property
    def ok(self) -> bool:
        return self.records > 0 and not self.failures

    def line(self) -> str:
        if not self.records:
            return "%-12s keine Daten im Abbild" % self.object_key
        state = "OK" if self.ok else "ABWEICHUNG"
        return ("%-12s %4d Datensaetze, Round-Trip %4d/%-4d %s"
                % (self.object_key, self.records, self.roundtrip_ok, self.records, state))


def verify_layout(img: MemoryImage, lay: Optional[Layout] = None) -> List[LayoutCheck]:
    """Prueft fuer jeden Datensatz, ob Decodieren+Codieren byteidentisch ist.

    Schlaegt das fehl, beschreibt das Layout diese Firmware nicht korrekt und
    feldbasiertes Schreiben wird gesperrt.
    """
    lay = lay or layout_mod.load()
    checks: List[LayoutCheck] = []
    for key, obj in lay.objects.items():
        check = LayoutCheck(key, obj.title)
        for idx in range(obj.count):
            try:
                addr = obj.address(idx)
            except IndexError:
                break
            if not img.has(addr, obj.record_size) or obj.hits_marker(idx):
                continue
            data = img.read(addr, obj.record_size)
            if set(data) <= {0x00, 0xFF}:
                continue          # unbenutzter Datensatz (Loeschmuster)
            check.records += 1
            try:
                if roundtrip_ok(obj, data):
                    check.roundtrip_ok += 1
                else:
                    check.failures.append(idx)
            except Exception:
                check.failures.append(idx)
        checks.append(check)
    return checks


# ---------------------------------------------------------------------------
# Decodieren in das Datenmodell
# ---------------------------------------------------------------------------

def decode(img: MemoryImage, lay: Optional[Layout] = None,
           log: Callable[[str], None] = lambda m: None) -> Codeplug:
    """Wandelt ein Speicherabbild in das bearbeitbare Datenmodell."""
    lay = lay or layout_mod.load()
    cp = Codeplug(name="Aus Geraet gelesen",
                  model=img.meta.get("model") or "AT-D878UV II Plus")
    cp.settings["firmware"] = img.meta.get("firmware", "")

    # --- Radio-IDs ------------------------------------------------------
    obj = lay.objects.get("radio_id")
    if obj:
        for idx in _used(img, lay, obj):
            addr = obj.address(idx)
            if not img.has(addr, obj.record_size):
                continue
            rec = decode_record(obj, img.read(addr, obj.record_size))
            name, rid = str(rec.get("name", "")), int(rec.get("radio_id") or 0)
            if rid or name:
                cp.radio_ids.append(RadioID(name=name or "ID%d" % rid, radio_id=rid, index=idx))

    # --- Kontakte -------------------------------------------------------
    contact_by_index: Dict[int, TalkGroup] = {}
    obj = lay.objects.get("contact")
    if obj:
        for idx in _used(img, lay, obj):
            addr = obj.address(idx)
            if not img.has(addr, obj.record_size):
                continue
            if obj.hits_marker(idx):
                continue          # Flash-Marke, kein Kontakt
            raw = img.read(addr, obj.record_size)
            if raw[:8] in (b"\xff" * 8, b"\x00" * 8):
                continue
            rec = decode_record(obj, raw)
            name = str(rec.get("name", "")).strip()
            tg_id = int(rec.get("tg_id") or 0)
            if not name and not tg_id:
                continue
            tg = TalkGroup(name=name or str(tg_id), tg_id=tg_id,
                           call_type=str(rec.get("call_type", "Group Call")),
                           call_alert=str(rec.get("call_alert", "None")), index=idx)
            cp.talkgroups.append(tg)
            contact_by_index[idx] = tg

    # --- Scanlisten (Namen zuerst, Mitglieder spaeter) -------------------
    scan_by_index: Dict[int, ScanList] = {}
    scan_members: Dict[int, List[int]] = {}
    obj = lay.objects.get("scan_list")
    if obj:
        for idx in _used(img, lay, obj):
            addr = obj.address(idx)
            if not img.has(addr, obj.record_size):
                continue
            rec = decode_record(obj, img.read(addr, obj.record_size))
            name = str(rec.get("name", "")).strip()
            if not name:
                continue
            sl = ScanList(name=name, index=idx,
                          look_back_a=int(rec.get("look_back_a") or 0) / 10.0,
                          look_back_b=int(rec.get("look_back_b") or 0) / 10.0,
                          dropout_delay=int(rec.get("dropout_delay") or 0) / 10.0,
                          dwell=int(rec.get("dwell") or 0) / 10.0)
            cp.scanlists.append(sl)
            scan_by_index[idx] = sl
            scan_members[idx] = list(rec.get("members") or [])

    # --- RX-Gruppenlisten -----------------------------------------------
    rxgroup_by_index: Dict[int, RxGroupList] = {}
    obj = lay.objects.get("rx_group")
    if obj:
        by_contact_index = {tg.index: tg for tg in cp.talkgroups if tg.index >= 0}
        for idx in range(obj.count):
            addr = obj.address(idx)
            if not img.has(addr, obj.record_size):
                continue
            raw = img.read(addr, obj.record_size)
            if set(raw) <= {0x00, 0xFF}:
                continue
            rec = decode_record(obj, raw)
            name = str(rec.get("name", "")).strip()
            members = [by_contact_index[m].name for m in rec.get("members", [])
                       if m in by_contact_index]
            if not name and not members:
                continue
            group = RxGroupList(name=name or "Gruppe %d" % (idx + 1), contacts=members)
            group.index = idx
            cp.rxgroups.append(group)
            rxgroup_by_index[idx] = group

    # --- Kanaele --------------------------------------------------------
    obj = lay.objects.get("channel")
    index_to_name: Dict[int, str] = {}
    if obj:
        for idx in _used(img, lay, obj):
            addr = obj.address(idx)
            if not img.has(addr, obj.record_size):
                continue
            rec = decode_record(obj, img.read(addr, obj.record_size))
            rx = _num(rec, "rx_freq")
            if not rx:
                continue
            ch = _channel_from_record(rec, idx, contact_by_index, scan_by_index,
                                      rxgroup_by_index)
            cp.channels.append(ch)
            index_to_name[idx] = ch.name

    for idx, members in scan_members.items():
        sl = scan_by_index.get(idx)
        if sl is not None:
            sl.channels = [index_to_name[m] for m in members if m in index_to_name]

    # --- Zonen ----------------------------------------------------------
    names_obj = lay.objects.get("zone_name")
    list_obj = lay.objects.get("zone_list")
    if names_obj and list_obj:
        for idx in _used(img, lay, names_obj):
            naddr, laddr = names_obj.address(idx), list_obj.address(idx)
            if not img.has(naddr, names_obj.record_size):
                continue
            roh = img.read(naddr, names_obj.record_size)
            if set(roh[:16]) <= {0xFF}:
                continue          # geloeschter Platz, keine echte Zone
            zname = str(decode_record(names_obj, roh).get("name", "")).strip()
            members: List[str] = []
            if img.has(laddr, list_obj.record_size):
                rec = decode_record(list_obj, img.read(laddr, list_obj.record_size))
                members = [index_to_name[m] for m in rec.get("members", []) if m in index_to_name]
            if zname or members:
                zone = Zone(name=zname or "Zone %d" % (idx + 1), channels=members,
                            a_channel=members[0] if members else "",
                            b_channel=members[0] if members else "", index=idx)
                zone.hide = "1" if _zone_hidden(img, lay, idx) else "0"
                cp.zones.append(zone)
    cp.settings["aprs"] = _decode_aprs(img, lay)
    log("Decodiert: %s" % ", ".join("%s=%d" % kv for kv in cp.stats().items()))
    return cp


APRS_FIELDS = ("fm_frequency", "tx_delay", "manual_interval", "auto_interval",
               "destination", "destination_ssid", "source", "source_ssid", "path",
               "symbol_table", "symbol", "fm_power")


def _decode_aprs(img: MemoryImage, lay: Layout) -> Dict[str, object]:
    """Liest die APRS-Einstellungen, soweit sie im Abbild enthalten sind."""
    out: Dict[str, object] = {}
    obj = lay.objects.get("aprs")
    if obj and img.has(obj.address(0), obj.record_size):
        rec = decode_record(obj, img.read(obj.address(0), obj.record_size))
        out.update({k: rec[k] for k in APRS_FIELDS if k in rec})
    msg = lay.objects.get("aprs_message")
    if msg and img.has(msg.address(0), msg.record_size):
        out["message"] = decode_record(msg, img.read(msg.address(0), msg.record_size))["text"]
    return out


def rebuild_contact_index(img: MemoryImage, lay: Optional[Layout] = None,
                          log: Callable[[str], None] = lambda m: None) -> MemoryImage:
    """Berechnet die Kontakt-Indextabelle neu.

    Das Geraet baut seine Kontaktliste aus dieser Tabelle bei 0x02600000 auf:
    je belegtem Kontakt ein uint32 mit dessen Platznummer, aufsteigend nach
    Kontakt-ID sortiert, danach 0xFF. Fehlt sie, zeigt das Geraet nach einem
    Schreibvorgang nur einen einzigen Kontakt - unabhaengig davon, wie viele
    Datensaetze geschrieben wurden.
    """
    lay = lay or layout_mod.load()
    region = lay.regions.get("contact_index")
    obj = lay.objects.get("contact")
    if region is None or obj is None:
        return img
    eintraege = []
    for idx in _used(img, lay, obj):
        addr = obj.address(idx)
        if not img.has(addr, obj.record_size):
            continue
        rec = decode_record(obj, img.read(addr, obj.record_size))
        tg_id = int(rec.get("tg_id") or 0)
        if not tg_id and not str(rec.get("name", "")).strip():
            continue
        eintraege.append((tg_id, idx))
    if not eintraege:
        return img
    eintraege.sort()
    tabelle = b"".join(struct.pack("<I", idx) for _, idx in eintraege)
    tabelle += b"\xff" * min(64, region.size - len(tabelle))
    out = img.copy()
    out.write(region.addr, tabelle)
    log("Kontakt-Indextabelle neu berechnet: %d Eintraege" % len(eintraege))
    return out


def patch_settings(img: MemoryImage, cp: Codeplug, lay: Optional[Layout] = None,
                   log: Callable[[str], None] = lambda m: None) -> MemoryImage:
    """Uebertraegt geaenderte APRS-Einstellungen in eine Kopie des Abbilds."""
    lay = lay or layout_mod.load()
    out = img.copy()
    aprs = (cp.settings or {}).get("aprs") or {}
    if not aprs:
        return out
    obj = lay.objects.get("aprs")
    touched = 0
    if obj and out.has(obj.address(0), obj.record_size):
        addr = obj.address(0)
        original = out.read(addr, obj.record_size)
        values = {k: aprs[k] for k in APRS_FIELDS if k in aprs and aprs[k] != ""}
        patched = encode_record(obj, values, original)
        if patched != original:
            out.write(addr, patched)
            touched += 1
    msg = lay.objects.get("aprs_message")
    if msg and "message" in aprs and out.has(msg.address(0), msg.record_size):
        addr = msg.address(0)
        original = out.read(addr, msg.record_size)
        patched = encode_record(msg, {"text": aprs["message"]}, original)
        if patched != original:
            out.write(addr, patched)
            touched += 1
    if touched:
        log("APRS-Einstellungen im Abbild aktualisiert")
    return out


def _num(rec: Dict[str, object], key: str, default: int = 0) -> int:
    """Zahlwert aus einem Datensatz - 0 ist ein gueltiger Wert, nicht 'leer'."""
    value = rec.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _channel_from_record(rec: Dict[str, object], idx: int,
                         contacts: Dict[int, TalkGroup],
                         scanlists: Dict[int, ScanList],
                         rxgroups: Optional[Dict[int, "RxGroupList"]] = None) -> Channel:
    """Baut einen Kanal aus dem decodierten Datensatz."""
    rx = _num(rec, "rx_freq")
    offset = _num(rec, "tx_offset")
    mode = str(rec.get("repeater", "Simplex"))
    if mode == "Plus":
        tx = rx + offset
    elif mode == "Minus":
        tx = rx - offset
    elif offset >= 100_000_000:
        tx = offset          # getrennte Sendefrequenz (odd split) statt Ablage
    else:
        tx = rx

    ch = Channel(
        name=str(rec.get("name", "")) or "CH%04d" % (idx + 1),
        rx_freq=rx, tx_freq=tx,
        ch_type=str(rec.get("ch_type", "D-Digital")),
        power=str(rec.get("power", "High")),
        bandwidth=str(rec.get("bandwidth", "12.5K")),
        color_code=_num(rec, "color_code"),
        slot=int(str(rec.get("slot", "1")) or 1),
        ctcss_encode=tone_from_index(_num(rec, "ctcss_encode_index"),
                                     str(rec.get("encode_type", "Off"))),
        ctcss_decode=tone_from_index(_num(rec, "ctcss_decode_index"),
                                     str(rec.get("decode_type", "Off"))))
    ch.squelch_mode = "CTCSS/DCS" if ch.ctcss_decode != "Off" else "Carrier"

    contact = contacts.get(_num(rec, "contact_index", -1))
    if contact and ch.ch_type != "A-Analog":
        ch.contact = contact.name
        ch.contact_id = contact.tg_id
        ch.contact_call_type = contact.call_type

    scan_idx = _num(rec, "scan_list_index", 0xFF)
    scan = scanlists.get(scan_idx)
    # Ist der Verweis unbekannt, bleibt er als '#n' sichtbar - so geht er beim
    # Zurueckschreiben nicht verloren.
    ch.scan_list = scan.name if scan else ("None" if scan_idx == 0xFF else "#%d" % scan_idx)

    rx_group = _num(rec, "rx_group_index", 0xFF)
    group = (rxgroups or {}).get(rx_group)
    ch.rx_group = group.name if group else ("None" if rx_group == 0xFF else "#%d" % rx_group)

    ch.extra["_index"] = str(idx)
    ch.extra["_offset"] = str(offset)
    ch.extra["_contact_index"] = str(_num(rec, "contact_index"))
    ch.extra["_scan_index"] = str(scan_idx)
    ch.extra["_rx_group_index"] = str(rx_group)
    ch.extra["_custom_ctcss"] = str(_num(rec, "custom_ctcss"))
    return ch


def _zone_hidden(img: MemoryImage, lay: Layout, idx: int) -> bool:
    """Ist die Zone am Geraet versteckt? (Bitmap 0x024C1360, gesetzt = versteckt)"""
    region = lay.regions.get("zone_hide")
    if region is None or not img.has(region.addr, region.size):
        return False
    return bitmap_get(img.read(region.addr, region.size), idx)


def set_zone_visibility(img: MemoryImage, sichtbar: Dict[int, bool],
                        lay: Optional[Layout] = None,
                        log: Callable[[str], None] = lambda m: None) -> MemoryImage:
    """Setzt die Sichtbarkeit von Zonen in einer Kopie des Abbilds."""
    lay = lay or layout_mod.load()
    region = lay.regions.get("zone_hide")
    if region is None:
        return img
    out = img.copy()
    bits = bytearray(out.read(region.addr, region.size)
                     if out.has(region.addr, region.size) else b"\xff" * region.size)
    for idx, zeigen in sichtbar.items():
        bitmap_set(bits, idx, not zeigen)          # gesetzt = versteckt
    out.write(region.addr, bytes(bits))
    log("Sichtbarkeit gesetzt fuer %d Zonen" % len(sichtbar))
    return out


def _used(img: MemoryImage, lay: Layout, obj: ObjectDef) -> List[int]:
    if obj.bitmap and obj.bitmap in lay.regions:
        region = lay.region(obj.bitmap)
        if img.has(region.addr, region.size):
            used = bitmap_indices(img.read(region.addr, region.size), obj.count,
                                  invert=region.invert)
            if used:
                return used
    return list(range(obj.count))


# ---------------------------------------------------------------------------
# Zurueckschreiben einzelner Felder (experimentell)
# ---------------------------------------------------------------------------

def field_write_allowed(img: MemoryImage, lay: Optional[Layout] = None) -> Tuple[bool, str]:
    """Darf feldweise zurueckgeschrieben werden?

    Nur wenn das Layout fuer jeden vorhandenen Datensatz den Round-Trip
    besteht - sonst beschreibt es diese Firmware nicht zuverlaessig.
    """
    checks = verify_layout(img, lay)
    bad = [c for c in checks if c.records and c.failures]
    if bad:
        return False, ("Layout passt nicht zu diesem Abbild: "
                       + "; ".join("%s (%d von %d abweichend)"
                                   % (c.object_key, len(c.failures), c.records) for c in bad))
    if not any(c.records for c in checks):
        return False, "Im Abbild sind keine decodierbaren Datensaetze enthalten"
    return True, "Layout bestaetigt (%s)" % ", ".join(
        "%s %d/%d" % (c.object_key, c.roundtrip_ok, c.records) for c in checks if c.records)


def patch_channels(img: MemoryImage, cp: Codeplug, lay: Optional[Layout] = None,
                   log: Callable[[str], None] = lambda m: None) -> MemoryImage:
    """Uebertraegt geaenderte Kanalfelder in eine Kopie des Abbilds.

    Nur Kanaele mit bekanntem Geraeteindex (``extra['_index']``) werden
    angefasst, und pro Datensatz nur die im Layout beschriebenen Bytes.
    Verweise (Kontakt, Scanliste, RX-Gruppe) werden ueber die beim Einlesen
    gemerkten Geraeteindizes aufgeloest; laesst sich ein Name nicht zuordnen,
    bleibt der urspruengliche Index stehen.
    """
    lay = lay or layout_mod.load()
    obj = lay.obj("channel")
    out = img.copy()
    contact_index = {tg.name: tg.index for tg in cp.talkgroups if tg.index >= 0}
    scan_index = {sl.name: sl.index for sl in cp.scanlists if sl.index >= 0}
    group_index = {g.name: g.index for g in cp.rxgroups if getattr(g, "index", -1) >= 0}
    touched = 0

    for ch in cp.channels:
        raw_idx = (ch.extra or {}).get("_index")
        if raw_idx is None:
            continue
        idx = int(raw_idx)
        addr = obj.address(idx)
        if not out.has(addr, obj.record_size):
            continue
        original = out.read(addr, obj.record_size)

        offset = ch.tx_freq - ch.rx_freq
        if offset == 0:
            # Bei Simplex ist das Ablagefeld ohne Bedeutung; der urspruengliche
            # Inhalt bleibt erhalten, statt ihn zu nullen.
            repeater, stored_offset = "Simplex", _kept(ch, "_offset", 0)
        elif offset > 0:
            repeater, stored_offset = "Plus", offset
        else:
            repeater, stored_offset = "Minus", -offset

        enc_kind, enc_idx = tone_to_index(ch.ctcss_encode)
        dec_kind, dec_idx = tone_to_index(ch.ctcss_decode)

        values = {
            "rx_freq": ch.rx_freq,
            "tx_offset": stored_offset,
            "repeater": repeater,
            "ch_type": ch.ch_type,
            "power": ch.power,
            "bandwidth": ch.bandwidth,
            "encode_type": enc_kind,
            "decode_type": dec_kind,
            "color_code": max(0, min(15, int(ch.color_code))),
            "slot": "2" if int(ch.slot) == 2 else "1",
            "name": ch.name,
            "contact_index": contact_index.get(ch.contact, _kept(ch, "_contact_index", 0)),
            "scan_list_index": _list_index(ch.scan_list, scan_index,
                                           _kept(ch, "_scan_index", 0xFF)),
            "rx_group_index": _list_index(ch.rx_group, group_index,
                                          _kept(ch, "_rx_group_index", 0xFF)),
        }
        # Ist die Signalisierung aus, ist das Indexbyte bedeutungslos und
        # enthaelt oft einen alten Wert - den lassen wir unangetastet.
        if enc_kind != "Off":
            values["ctcss_encode_index"] = enc_idx
        if dec_kind != "Off":
            values["ctcss_decode_index"] = dec_idx

        patched = encode_record(obj, values, original)
        if patched != original:
            out.write(addr, patched)
            touched += 1
    log("%d Kanaele im Abbild aktualisiert" % touched)
    return out


def _kept(ch: Channel, key: str, default: int) -> int:
    """Beim Einlesen gemerkter Rohwert, falls vorhanden."""
    try:
        return int((ch.extra or {})[key])
    except (KeyError, TypeError, ValueError):
        return default


def _list_index(name: str, table: Dict[str, int], fallback: int) -> int:
    """Name -> Geraeteindex; '#n' bleibt ein roher Index, 'None' heisst keine."""
    text = str(name or "").strip()
    if not text or text.lower() in ("none", "-"):
        return 0xFF
    if text in table:
        return table[text]
    return _raw_index(text, fallback)


def _raw_index(name: str, fallback: int) -> int:
    """Verweise ohne bekannte Namenstabelle werden als '#n' gefuehrt."""
    text = str(name or "").strip()
    if not text or text.lower() == "none":
        return 0xFF
    if text.startswith("#") and text[1:].isdigit():
        return int(text[1:])
    return fallback
