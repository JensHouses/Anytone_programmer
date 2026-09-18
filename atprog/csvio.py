"""CSV-Import/Export im Format der offiziellen AnyTone-CPS (Version 3.x/4.x).

Exportierte Dateien lassen sich unveraendert in die CPS einlesen, importierte
CPS-Dateien werden verlustfrei uebernommen: unbekannte Spalten wandern nach
``Channel.extra`` und werden beim Export wieder ausgegeben.
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    Channel, Codeplug, RadioID, RxGroupList, ScanList, TalkGroup, Zone,
    clean_name, fmt_freq, norm_tone, parse_freq,
)

SEP = "|"          # Trennzeichen fuer Listen innerhalb einer Zelle
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

# (CSV-Spalte, Attribut, Typ) - Typ: str|freq|int|tone
CHANNEL_MAP: Sequence[Tuple[str, Optional[str], str]] = (
    ("No.", None, "int"),
    ("Channel Name", "name", "str"),
    ("Receive Frequency", "rx_freq", "freq"),
    ("Transmit Frequency", "tx_freq", "freq"),
    ("Channel Type", "ch_type", "str"),
    ("Transmit Power", "power", "str"),
    ("Band Width", "bandwidth", "str"),
    ("CTCSS/DCS Decode", "ctcss_decode", "tone"),
    ("CTCSS/DCS Encode", "ctcss_encode", "tone"),
    ("Contact", "contact", "str"),
    ("Contact Call Type", "contact_call_type", "str"),
    ("Contact TG/DMR ID", "contact_id", "int"),
    ("Radio ID", "radio_id", "str"),
    ("Busy Lock/TX Permit", "tx_permit", "str"),
    ("Squelch Mode", "squelch_mode", "str"),
    ("Optional Signal", None, "str"),
    ("DTMF ID", None, "str"),
    ("2Tone ID", None, "str"),
    ("5Tone ID", None, "str"),
    ("PTT ID", None, "str"),
    ("Color Code", "color_code", "int"),
    ("Slot", "slot", "int"),
    ("Scan List", "scan_list", "str"),
    ("Receive Group List", "rx_group", "str"),
    ("PTT Prohibit", "ptt_prohibit", "str"),
    ("Reverse", "reverse", "str"),
    ("Simplex TDMA", None, "str"),
    ("Slot Suit", None, "str"),
    ("AES Digital Encryption", None, "str"),
    ("Digital Encryption", None, "str"),
    ("Call Confirmation", "call_confirmation", "str"),
    ("Talk Around(Simplex)", "talkaround", "str"),
    ("Work Alone", "work_alone", "str"),
    ("Custom CTCSS", None, "str"),
    ("2TONE Decode", None, "str"),
    ("Ranging", None, "str"),
    ("Through Mode", None, "str"),
    ("Digi APRS RX", None, "str"),
    ("Analog APRS PTT Mode", None, "str"),
    ("Digital APRS PTT Mode", None, "str"),
    ("APRS Report Type", "aprs_report", "str"),
    ("Digital APRS Report Channel", "aprs_report_channel", "int"),
    ("Correct Frequency[Hz]", "correct_frequency", "int"),
    ("SMS Confirmation", None, "str"),
    ("Exclude channel from roaming", "exclude_from_roaming", "str"),
    ("DMR MODE", "dmr_mode", "str"),
    ("DataACK Disable", None, "str"),
    ("R5toneBot", None, "str"),
    ("R5ToneEot", None, "str"),
    ("Auto Scan", None, "str"),
    ("Ana Aprs Mute", None, "str"),
    ("Send Talker Alias", None, "str"),
    ("AnaAprsTxPath", None, "str"),
    ("ARC4", None, "str"),
    ("ex_emg_kind", None, "str"),
)

# Vorgaben fuer Spalten, die dieses Programm nicht modelliert.
CHANNEL_DEFAULTS: Dict[str, str] = {
    "Optional Signal": "Off", "DTMF ID": "1", "2Tone ID": "1", "5Tone ID": "1",
    "PTT ID": "Off", "Simplex TDMA": "Off", "Slot Suit": "Off",
    "AES Digital Encryption": "Normal Encryption", "Digital Encryption": "Off",
    "Custom CTCSS": "251.1", "2TONE Decode": "0", "Ranging": "Off",
    "Through Mode": "Off", "Digi APRS RX": "Off", "Analog APRS PTT Mode": "Off",
    "Digital APRS PTT Mode": "Off", "SMS Confirmation": "Off",
    "DataACK Disable": "0", "R5toneBot": "0", "R5ToneEot": "0", "Auto Scan": "0",
    "Ana Aprs Mute": "0", "Send Talker Alias": "0", "AnaAprsTxPath": "0",
    "ARC4": "0", "ex_emg_kind": "0",
}

CHANNEL_COLUMNS = [c for c, _, _ in CHANNEL_MAP]
TALKGROUP_COLUMNS = ["No.", "Radio ID", "Name", "Call Type", "Call Alert"]
RADIOID_COLUMNS = ["No.", "Radio ID", "Name"]
ZONE_COLUMNS = ["No.", "Zone Name", "Zone Channel Member",
                "Zone Channel Member RX Frequency", "Zone Channel Member TX Frequency",
                "A Channel", "A Channel RX Frequency", "A Channel TX Frequency",
                "B Channel", "B Channel RX Frequency", "B Channel TX Frequency", "Zone Hide "]
RXGROUP_COLUMNS = ["No.", "Group Name", "Contact", "Contact TG/DMR ID"]
SCANLIST_COLUMNS = ["No.", "Scan List Name", "Scan Channel Member",
                    "Scan Channel Member RX Frequency", "Scan Channel Member TX Frequency",
                    "Scan Mode", "Priority Channel Select", "Priority Channel 1",
                    "Priority Channel 1 RX Frequency", "Priority Channel 1 TX Frequency",
                    "Priority Channel 2", "Priority Channel 2 RX Frequency",
                    "Priority Channel 2 TX Frequency", "Revert Channel",
                    "Look Back Time A[s]", "Look Back Time B[s]", "Dropout Delay Time[s]",
                    "Dwell Time[s]"]

FILENAMES = {
    "channels": "Channel.CSV",
    "talkgroups": "TalkGroups.CSV",
    "zones": "Zone.CSV",
    "scanlists": "ScanList.CSV",
    "rxgroups": "ReceiveGroupCallList.CSV",
    "radio_ids": "RadioIDList.CSV",
}


class CsvError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Low-Level
# ---------------------------------------------------------------------------

def _read_rows(path: str) -> List[Dict[str, str]]:
    last: Optional[Exception] = None
    for enc in ENCODINGS:
        try:
            with io.open(path, "r", encoding=enc, newline="") as fh:
                text = fh.read()
            break
        except UnicodeDecodeError as exc:
            last = exc
    else:
        raise CsvError("Datei %s ist in keiner bekannten Codierung lesbar (%s)" % (path, last))
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        delim = dialect.delimiter
    except Exception:
        delim = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows: List[Dict[str, str]] = []
    for raw in reader:
        row = {(k or "").strip(): (v if v is not None else "") for k, v in raw.items()}
        if any(str(v).strip() for v in row.values()):
            rows.append(row)
    return rows


def _write_rows(path: str, columns: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore",
                                lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def _pick(row: Dict[str, str], *names: str) -> str:
    """Spalte tolerant suchen (Gross-/Kleinschreibung, Leerzeichen egal)."""
    norm = {k.strip().lower().replace(" ", ""): v for k, v in row.items()}
    for name in names:
        key = name.strip().lower().replace(" ", "")
        if key in norm:
            return str(norm[key] or "").strip()
    return ""


def _split_list(text: str) -> List[str]:
    if not text:
        return []
    parts = [p.strip() for p in str(text).split(SEP)]
    return [p for p in parts if p and p.lower() != "none"]


def _int(text: Any, default: int = 0) -> int:
    try:
        s = str(text).strip()
        if not s:
            return default
        return int(float(s)) if ("." in s or "e" in s.lower()) else int(s)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Kanaele
# ---------------------------------------------------------------------------

def channels_to_rows(cp: Codeplug) -> List[Dict[str, Any]]:
    rows = []
    for idx, ch in enumerate(cp.channels, 1):
        row: Dict[str, Any] = dict(CHANNEL_DEFAULTS)
        # Schluessel mit fuehrendem Unterstrich sind interne Vermerke
        # (z.B. der Geraeteindex) und gehoeren nicht in eine CPS-Datei.
        row.update({k: v for k, v in (ch.extra or {}).items() if not k.startswith("_")})
        row["No."] = idx
        for column, attr, kind in CHANNEL_MAP:
            if attr is None:
                continue
            value = getattr(ch, attr)
            if kind == "freq":
                row[column] = fmt_freq(value)
            else:
                row[column] = value
        row["Contact TG/DMR ID"] = ch.contact_id or (
            cp.talkgroup_by_name(ch.contact).tg_id if cp.talkgroup_by_name(ch.contact) else "")
        rows.append(row)
    return rows


def rows_to_channels(rows: Sequence[Dict[str, str]]) -> List[Channel]:
    known = {c for c, attr, _ in CHANNEL_MAP if attr is not None} | {"No."}
    out: List[Channel] = []
    for n, row in enumerate(rows, 1):
        ch = Channel()
        for column, attr, kind in CHANNEL_MAP:
            if attr is None:
                continue
            raw = _pick(row, column)
            if raw == "" and attr not in ("name",):
                continue
            if kind == "freq":
                try:
                    setattr(ch, attr, parse_freq(raw))
                except ValueError as exc:
                    raise CsvError("Zeile %d: %s" % (n, exc))
            elif kind == "int":
                setattr(ch, attr, _int(raw))
            elif kind == "tone":
                setattr(ch, attr, norm_tone(raw))
            else:
                setattr(ch, attr, raw)
        ch.name = clean_name(ch.name)
        if not ch.tx_freq:
            ch.tx_freq = ch.rx_freq
        ch.extra = {k: v for k, v in row.items() if k and k not in known}
        if ch.name or ch.rx_freq:
            out.append(ch)
    return out


def write_channels(path: str, cp: Codeplug) -> None:
    extra_cols: List[str] = []
    for ch in cp.channels:
        for key in (ch.extra or {}):
            if key.startswith("_") or key in CHANNEL_COLUMNS or key in extra_cols:
                continue
            extra_cols.append(key)
    _write_rows(path, CHANNEL_COLUMNS + extra_cols, channels_to_rows(cp))


def read_channels(path: str) -> List[Channel]:
    return rows_to_channels(_read_rows(path))


# ---------------------------------------------------------------------------
# Talkgroups / Radio-IDs
# ---------------------------------------------------------------------------

def write_talkgroups(path: str, cp: Codeplug) -> None:
    rows = [{"No.": i, "Radio ID": tg.tg_id, "Name": tg.name,
             "Call Type": tg.call_type, "Call Alert": tg.call_alert}
            for i, tg in enumerate(cp.talkgroups, 1)]
    _write_rows(path, TALKGROUP_COLUMNS, rows)


def read_talkgroups(path: str) -> List[TalkGroup]:
    out = []
    for row in _read_rows(path):
        tg = TalkGroup(
            name=clean_name(_pick(row, "Name", "Contact Name", "Alias"), 16),
            tg_id=_int(_pick(row, "Radio ID", "TG/DMR ID", "ID", "DMR ID")),
            call_type=_pick(row, "Call Type") or "Group Call",
            call_alert=_pick(row, "Call Alert") or "None")
        if tg.name or tg.tg_id:
            out.append(tg)
    return out


def write_radio_ids(path: str, cp: Codeplug) -> None:
    rows = [{"No.": i, "Radio ID": r.radio_id, "Name": r.name}
            for i, r in enumerate(cp.radio_ids, 1)]
    _write_rows(path, RADIOID_COLUMNS, rows)


def read_radio_ids(path: str) -> List[RadioID]:
    out = []
    for row in _read_rows(path):
        rid = RadioID(name=clean_name(_pick(row, "Name", "Radio ID Name"), 16),
                      radio_id=_int(_pick(row, "Radio ID", "ID", "DMR ID")))
        if rid.radio_id:
            out.append(rid)
    return out


# ---------------------------------------------------------------------------
# Zonen / Scanlisten / RX-Gruppen
# ---------------------------------------------------------------------------

def _member_freqs(cp: Codeplug, names: Sequence[str], which: str) -> str:
    vals = []
    for name in names:
        ch = cp.channel_by_name(name)
        vals.append(fmt_freq(getattr(ch, which)) if ch else "")
    return SEP.join(vals)


def write_zones(path: str, cp: Codeplug) -> None:
    rows = []
    for i, z in enumerate(cp.zones, 1):
        a = cp.channel_by_name(z.a_channel)
        b = cp.channel_by_name(z.b_channel)
        rows.append({
            "No.": i, "Zone Name": z.name,
            "Zone Channel Member": SEP.join(z.channels),
            "Zone Channel Member RX Frequency": _member_freqs(cp, z.channels, "rx_freq"),
            "Zone Channel Member TX Frequency": _member_freqs(cp, z.channels, "tx_freq"),
            "A Channel": z.a_channel,
            "A Channel RX Frequency": fmt_freq(a.rx_freq) if a else "",
            "A Channel TX Frequency": fmt_freq(a.tx_freq) if a else "",
            "B Channel": z.b_channel,
            "B Channel RX Frequency": fmt_freq(b.rx_freq) if b else "",
            "B Channel TX Frequency": fmt_freq(b.tx_freq) if b else "",
            "Zone Hide ": z.hide,
        })
    _write_rows(path, ZONE_COLUMNS, rows)


def read_zones(path: str) -> List[Zone]:
    out = []
    for row in _read_rows(path):
        members = _split_list(_pick(row, "Zone Channel Member", "Channel Member"))
        z = Zone(name=clean_name(_pick(row, "Zone Name", "Name"), 16), channels=members,
                 a_channel=_pick(row, "A Channel"), b_channel=_pick(row, "B Channel"),
                 hide=_pick(row, "Zone Hide", "Zone Hide ") or "0")
        if not z.a_channel and members:
            z.a_channel = members[0]
        if not z.b_channel and members:
            z.b_channel = members[0]
        if z.name:
            out.append(z)
    return out


def write_scanlists(path: str, cp: Codeplug) -> None:
    rows = []
    for i, s in enumerate(cp.scanlists, 1):
        rows.append({
            "No.": i, "Scan List Name": s.name,
            "Scan Channel Member": SEP.join(s.channels),
            "Scan Channel Member RX Frequency": _member_freqs(cp, s.channels, "rx_freq"),
            "Scan Channel Member TX Frequency": _member_freqs(cp, s.channels, "tx_freq"),
            "Scan Mode": "Off",
            "Priority Channel Select": "Off" if s.priority_ch1 == "Off" else "Priority Channel Select1",
            "Priority Channel 1": s.priority_ch1, "Priority Channel 1 RX Frequency": "",
            "Priority Channel 1 TX Frequency": "", "Priority Channel 2": s.priority_ch2,
            "Priority Channel 2 RX Frequency": "", "Priority Channel 2 TX Frequency": "",
            "Revert Channel": s.revert_channel,
            "Look Back Time A[s]": s.look_back_a, "Look Back Time B[s]": s.look_back_b,
            "Dropout Delay Time[s]": s.dropout_delay, "Dwell Time[s]": s.dwell,
        })
    _write_rows(path, SCANLIST_COLUMNS, rows)


def read_scanlists(path: str) -> List[ScanList]:
    out = []
    for row in _read_rows(path):
        s = ScanList(name=clean_name(_pick(row, "Scan List Name", "Name"), 16),
                     channels=_split_list(_pick(row, "Scan Channel Member")),
                     priority_ch1=_pick(row, "Priority Channel 1") or "Off",
                     priority_ch2=_pick(row, "Priority Channel 2") or "Off",
                     revert_channel=_pick(row, "Revert Channel") or "Selected")
        if s.name:
            out.append(s)
    return out


def write_rxgroups(path: str, cp: Codeplug) -> None:
    rows = []
    for i, g in enumerate(cp.rxgroups, 1):
        ids = []
        for name in g.contacts:
            tg = cp.talkgroup_by_name(name)
            ids.append(str(tg.tg_id) if tg else "")
        rows.append({"No.": i, "Group Name": g.name, "Contact": SEP.join(g.contacts),
                     "Contact TG/DMR ID": SEP.join(ids)})
    _write_rows(path, RXGROUP_COLUMNS, rows)


def read_rxgroups(path: str) -> List[RxGroupList]:
    out = []
    for row in _read_rows(path):
        g = RxGroupList(name=clean_name(_pick(row, "Group Name", "Name"), 16),
                        contacts=_split_list(_pick(row, "Contact")))
        if g.name:
            out.append(g)
    return out


# ---------------------------------------------------------------------------
# Sammel-Import/Export
# ---------------------------------------------------------------------------

def export_all(directory: str, cp: Codeplug) -> List[str]:
    os.makedirs(directory, exist_ok=True)
    written = []
    jobs = (
        ("channels", write_channels), ("talkgroups", write_talkgroups),
        ("zones", write_zones), ("scanlists", write_scanlists),
        ("rxgroups", write_rxgroups), ("radio_ids", write_radio_ids),
    )
    for key, fn in jobs:
        path = os.path.join(directory, FILENAMES[key])
        fn(path, cp)
        written.append(path)
    return written


def _find(directory: str, *candidates: str) -> Optional[str]:
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    lower = {e.lower(): e for e in entries}
    for cand in candidates:
        if cand.lower() in lower:
            return os.path.join(directory, lower[cand.lower()])
    return None


def import_all(directory: str, cp: Optional[Codeplug] = None) -> Tuple[Codeplug, List[str]]:
    """Liest alle vorhandenen CPS-CSV-Dateien eines Verzeichnisses."""
    cp = cp or Codeplug()
    loaded: List[str] = []
    jobs = (
        ("radio_ids", ("RadioIDList.CSV", "RadioID.CSV"), read_radio_ids),
        ("talkgroups", ("TalkGroups.CSV", "TalkGroup.CSV", "Contacts.CSV"), read_talkgroups),
        ("channels", ("Channel.CSV", "Channels.CSV"), read_channels),
        ("zones", ("Zone.CSV", "Zones.CSV"), read_zones),
        ("scanlists", ("ScanList.CSV", "ScanLists.CSV"), read_scanlists),
        ("rxgroups", ("ReceiveGroupCallList.CSV", "RXGroupList.CSV"), read_rxgroups),
    )
    for attr, names, reader in jobs:
        path = _find(directory, *names)
        if path:
            setattr(cp, attr, reader(path))
            loaded.append(os.path.basename(path))
    return cp, loaded


def import_dmr_id_database(path: str, cp: Codeplug, limit: int = 0,
                           country: str = "") -> int:
    """Importiert eine RadioID.net-Nutzerliste als Einzelruf-Kontakte."""
    added = 0
    for row in _read_rows(path):
        rid = _int(_pick(row, "RADIO_ID", "Radio ID", "ID", "DMR ID"))
        if not rid:
            continue
        if country and _pick(row, "COUNTRY", "Country").lower() != country.lower():
            continue
        call = _pick(row, "CALLSIGN", "Callsign", "Call")
        name = _pick(row, "NAME", "Name", "First Name")
        label = clean_name(("%s %s" % (call, name)).strip() or str(rid), 16)
        if cp.talkgroup_by_id(rid):
            continue
        cp.talkgroups.append(TalkGroup(name=label, tg_id=rid, call_type="Private Call"))
        added += 1
        if limit and added >= limit:
            break
    return added
