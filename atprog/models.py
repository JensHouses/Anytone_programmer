"""Datenmodell eines AT-D878UV II Plus Codeplugs.

Die Feldwerte entsprechen bewusst den Klartext-Werten der offiziellen CPS-CSV
Dateien (z.B. Power "Turbo", Bandbreite "12.5K"), damit Import und Export
verlustfrei bleiben. Frequenzen werden intern immer als ganze Hertz gehalten.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields as dc_fields
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Konstanten / zulaessige Werte
# ---------------------------------------------------------------------------

RX_BANDS = ((100_000_000, 174_000_000), (220_000_000, 260_000_000), (350_000_000, 520_000_000))
TX_BANDS = ((136_000_000, 174_000_000), (400_000_000, 480_000_000))

CTCSS_TONES = [
    62.5, 67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8,
    97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8,
    136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3,
    173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5,
    206.5, 210.7, 213.8, 218.1, 221.3, 225.7, 229.1, 233.6, 237.1, 241.8,
    245.5, 250.3, 254.1,
]
DCS_CODES = [
    23, 25, 26, 31, 32, 36, 43, 47, 51, 53, 54, 65, 71, 72, 73, 74, 114, 115,
    116, 122, 125, 131, 132, 134, 143, 145, 152, 155, 156, 162, 165, 172, 174,
    205, 212, 223, 225, 226, 243, 244, 245, 246, 251, 252, 255, 261, 263, 265,
    266, 271, 274, 306, 311, 315, 325, 331, 332, 343, 346, 351, 356, 364, 365,
    371, 411, 412, 413, 423, 431, 432, 445, 446, 452, 454, 455, 462, 464, 465,
    466, 503, 506, 516, 523, 526, 532, 546, 565, 606, 612, 624, 627, 631, 632,
    645, 654, 662, 664, 703, 712, 723, 731, 732, 734, 743, 754,
]

CHANNEL_TYPES = ("A-Analog", "D-Digital", "D+A", "A+D")
POWER_LEVELS = ("Turbo", "High", "Mid", "Low")
BANDWIDTHS = ("12.5K", "25K")
CALL_TYPES = ("Group Call", "Private Call", "All Call")
TX_PERMITS = ("Always", "ChannelFree", "Different Colorcode", "Same Colorcode")
SQUELCH_MODES = ("Carrier", "CTCSS/DCS")
ON_OFF = ("Off", "On")

MAX_CHANNELS = 4000
MAX_TALKGROUPS = 10000
MAX_ZONES = 250
MAX_ZONE_MEMBERS = 250
MAX_SCANLISTS = 250
MAX_SCAN_MEMBERS = 50
MAX_RXGROUPS = 250
MAX_RXGROUP_MEMBERS = 64
MAX_RADIO_IDS = 250
NAME_LEN = 16


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def parse_freq(value: Any) -> int:
    """Nimmt 438.5, '438.500000', '438500000', '438,500' -> Hertz (int)."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return int(value) if value > 1_000_000 else int(round(value * 1_000_000))
    if isinstance(value, float):
        return int(round(value * 1_000_000)) if value < 10_000 else int(round(value))
    text = str(value).strip().replace(",", ".")
    if not text:
        return 0
    text = re.sub(r"(?i)\s*mhz$", "", text).strip()
    try:
        num = float(text)
    except ValueError:
        raise ValueError("Ungueltige Frequenz: %r" % value)
    if "." in text or num < 10_000:
        return int(round(num * 1_000_000))
    return int(round(num))


def fmt_freq(hz: int, decimals: int = 5) -> str:
    """Formatiert Hertz als MHz-String im CPS-Format (438.50000)."""
    if not hz:
        return ""
    return ("%%.%df" % decimals) % (hz / 1_000_000.0)


def in_band(hz: int, bands: Iterable[Tuple[int, int]]) -> bool:
    return any(lo <= hz <= hi for lo, hi in bands)


def norm_tone(value: Any) -> str:
    """Normalisiert CTCSS/DCS-Angaben auf CPS-Schreibweise."""
    if value is None:
        return "Off"
    text = str(value).strip()
    if text == "" or text.lower() in ("off", "none", "-"):
        return "Off"
    upper = text.upper()
    m = re.fullmatch(r"D?(\d{1,3})\s*([NI])?", upper)
    if m and (upper.startswith("D") or (m.group(2) and int(m.group(1)) in DCS_CODES)):
        return "D%03d%s" % (int(m.group(1)), m.group(2) or "N")
    try:
        return "%.1f" % float(text.replace(",", "."))
    except ValueError:
        return text


def tone_is_valid(tone: str) -> bool:
    if tone == "Off":
        return True
    m = re.fullmatch(r"D(\d{3})([NI])", tone)
    if m:
        return int(m.group(1)) in DCS_CODES
    try:
        return round(float(tone), 1) in [round(t, 1) for t in CTCSS_TONES]
    except ValueError:
        return False


def clean_name(text: Any, limit: int = NAME_LEN) -> str:
    out = str(text or "").strip()
    out = out.replace("\r", " ").replace("\n", " ").replace(",", " ").replace("|", "/")
    out = re.sub(r"\s{2,}", " ", out)
    return out[:limit]


def tone_from_index(index: int, kind: str) -> str:
    """Wandelt Geraeteindex + Signalisierungsart in die CPS-Schreibweise."""
    if kind == "CTCSS":
        return "%.1f" % CTCSS_TONES[index] if 0 <= index < len(CTCSS_TONES) else "Off"
    if kind in ("DCS-N", "DCS-I"):
        suffix = "N" if kind == "DCS-N" else "I"
        return "D%03d%s" % (DCS_CODES[index], suffix) if 0 <= index < len(DCS_CODES) else "Off"
    return "Off"


def tone_to_index(tone: str) -> Tuple[str, int]:
    """Umkehrung von :func:`tone_from_index`: liefert (Art, Index)."""
    tone = norm_tone(tone)
    if tone == "Off":
        return "Off", 0
    m = re.fullmatch(r"D(\d{3})([NI])", tone)
    if m:
        code = int(m.group(1))
        kind = "DCS-N" if m.group(2) == "N" else "DCS-I"
        return (kind, DCS_CODES.index(code)) if code in DCS_CODES else ("Off", 0)
    try:
        value = round(float(tone), 1)
    except ValueError:
        return "Off", 0
    for i, t in enumerate(CTCSS_TONES):
        if round(t, 1) == value:
            return "CTCSS", i
    return "Off", 0


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        text = str(value).strip()
        return int(text) if text else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Einzelobjekte
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    name: str = ""
    rx_freq: int = 0                 # Hertz
    tx_freq: int = 0                 # Hertz
    ch_type: str = "D-Digital"
    power: str = "High"
    bandwidth: str = "12.5K"
    ctcss_decode: str = "Off"        # RX-Ton (analog)
    ctcss_encode: str = "Off"        # TX-Ton (analog)
    contact: str = ""                # Name der Standard-Talkgroup
    contact_call_type: str = "Group Call"
    contact_id: int = 0
    radio_id: str = ""               # Name aus der Radio-ID-Liste
    tx_permit: str = "Always"
    squelch_mode: str = "Carrier"
    color_code: int = 1
    slot: int = 1
    scan_list: str = "None"
    rx_group: str = "None"
    ptt_prohibit: str = "Off"        # = RX only
    reverse: str = "Off"
    talkaround: str = "Off"
    call_confirmation: str = "Off"
    work_alone: str = "Off"
    aprs_report: str = "Off"
    aprs_report_channel: int = 1
    correct_frequency: int = 0       # Hz-Korrektur
    exclude_from_roaming: str = "0"
    dmr_mode: str = ""
    extra: Dict[str, str] = field(default_factory=dict)

    # -- abgeleitete Groessen -------------------------------------------
    @property
    def is_digital(self) -> bool:
        return self.ch_type in ("D-Digital", "D+A", "A+D")

    @property
    def offset(self) -> int:
        return self.tx_freq - self.rx_freq

    def set_offset(self, offset_hz: int) -> None:
        self.tx_freq = self.rx_freq + offset_hz

    def label(self) -> str:
        return self.name or fmt_freq(self.rx_freq)

    def to_dict(self) -> Dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in dc_fields(self)}
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Channel":
        known = {f.name for f in dc_fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        for key in ("rx_freq", "tx_freq"):
            if key in kwargs:
                kwargs[key] = parse_freq(kwargs[key])
        for key in ("color_code", "slot", "contact_id", "aprs_report_channel", "correct_frequency"):
            if key in kwargs:
                kwargs[key] = _coerce_int(kwargs[key])
        kwargs.setdefault("extra", {})
        kwargs["extra"] = dict(kwargs.get("extra") or {})
        return cls(**kwargs)


@dataclass
class TalkGroup:
    """Digitaler Kontakt (Talkgroup oder Einzelruf-ID)."""
    name: str = ""
    tg_id: int = 0
    call_type: str = "Group Call"
    call_alert: str = "None"
    index: int = -1          # Platz im Geraet, -1 = noch nicht zugeordnet

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "tg_id": self.tg_id, "call_type": self.call_type,
                "call_alert": self.call_alert, "index": self.index}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TalkGroup":
        return cls(name=str(d.get("name", "")), tg_id=_coerce_int(d.get("tg_id")),
                   call_type=str(d.get("call_type") or "Group Call"),
                   call_alert=str(d.get("call_alert") or "None"),
                   index=_coerce_int(d.get("index"), -1))


@dataclass
class RadioID:
    name: str = ""
    radio_id: int = 0
    index: int = -1

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "radio_id": self.radio_id, "index": self.index}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RadioID":
        return cls(name=str(d.get("name", "")), radio_id=_coerce_int(d.get("radio_id")),
                   index=_coerce_int(d.get("index"), -1))


@dataclass
class Zone:
    name: str = ""
    channels: List[str] = field(default_factory=list)
    a_channel: str = ""
    b_channel: str = ""
    hide: str = "0"
    index: int = -1

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "channels": list(self.channels),
                "a_channel": self.a_channel, "b_channel": self.b_channel,
                "hide": self.hide, "index": self.index}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Zone":
        return cls(name=str(d.get("name", "")), channels=list(d.get("channels") or []),
                   a_channel=str(d.get("a_channel", "")), b_channel=str(d.get("b_channel", "")),
                   hide=str(d.get("hide", "0")), index=_coerce_int(d.get("index"), -1))


@dataclass
class RxGroupList:
    name: str = ""
    contacts: List[str] = field(default_factory=list)
    index: int = -1

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "contacts": list(self.contacts), "index": self.index}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RxGroupList":
        return cls(name=str(d.get("name", "")), contacts=list(d.get("contacts") or []),
                   index=_coerce_int(d.get("index"), -1))


@dataclass
class ScanList:
    name: str = ""
    channels: List[str] = field(default_factory=list)
    index: int = -1
    priority_ch1: str = "Off"
    priority_ch2: str = "Off"
    revert_channel: str = "Selected"
    look_back_a: float = 2.0
    look_back_b: float = 3.0
    dropout_delay: float = 3.1
    dwell: float = 3.1

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in dc_fields(self)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScanList":
        known = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Issue:
    level: str          # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return "[%s] %s: %s" % (self.level.upper(), self.where, self.message)


# ---------------------------------------------------------------------------
# Codeplug
# ---------------------------------------------------------------------------

@dataclass
class Codeplug:
    """Vollstaendiger Datensatz, wie ihn das Geraet/die CPS kennt."""
    name: str = "Codeplug"
    model: str = "AT-D878UV II Plus"
    channels: List[Channel] = field(default_factory=list)
    talkgroups: List[TalkGroup] = field(default_factory=list)
    zones: List[Zone] = field(default_factory=list)
    scanlists: List[ScanList] = field(default_factory=list)
    rxgroups: List[RxGroupList] = field(default_factory=list)
    radio_ids: List[RadioID] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=dict)

    # -- Suchen ----------------------------------------------------------
    def channel_by_name(self, name: str) -> Optional[Channel]:
        key = (name or "").strip().lower()
        for ch in self.channels:
            if ch.name.strip().lower() == key:
                return ch
        return None

    def talkgroup_by_name(self, name: str) -> Optional[TalkGroup]:
        key = (name or "").strip().lower()
        for tg in self.talkgroups:
            if tg.name.strip().lower() == key:
                return tg
        return None

    def talkgroup_by_id(self, tg_id: int) -> Optional[TalkGroup]:
        for tg in self.talkgroups:
            if tg.tg_id == tg_id:
                return tg
        return None

    def zone_by_name(self, name: str) -> Optional[Zone]:
        key = (name or "").strip().lower()
        for z in self.zones:
            if z.name.strip().lower() == key:
                return z
        return None

    # -- Bearbeiten ------------------------------------------------------
    def add_channel(self, ch: Channel) -> Channel:
        self.channels.append(ch)
        return ch

    def ensure_talkgroup(self, name: str, tg_id: int,
                         call_type: str = "Group Call") -> TalkGroup:
        existing = self.talkgroup_by_id(tg_id)
        if existing:
            return existing
        tg = TalkGroup(name=clean_name(name), tg_id=tg_id, call_type=call_type)
        self.talkgroups.append(tg)
        return tg

    def rename_channel(self, old: str, new: str) -> int:
        """Benennt einen Kanal um und zieht alle Verweise mit.

        Zonen, Scanlisten sowie A-/B-Kanal einer Zone zeigen auf Kanalnamen -
        ohne Nachfuehren wuerde eine Umbenennung diese Verweise zerreissen.
        """
        if not old or old == new:
            return 0
        touched = 0
        for z in self.zones:
            z.channels = [new if c == old else c for c in z.channels]
            if z.a_channel == old:
                z.a_channel = new
                touched += 1
            if z.b_channel == old:
                z.b_channel = new
                touched += 1
            touched += sum(1 for c in z.channels if c == new)
        for s in self.scanlists:
            s.channels = [new if c == old else c for c in s.channels]
            for attr in ("priority_ch1", "priority_ch2"):
                if getattr(s, attr) == old:
                    setattr(s, attr, new)
                    touched += 1
            touched += sum(1 for c in s.channels if c == new)
        return touched

    def rename_talkgroup(self, old: str, new: str) -> int:
        """Benennt einen Kontakt um und zieht Kanal- und Gruppenverweise mit."""
        if not old or old == new:
            return 0
        touched = 0
        for ch in self.channels:
            if ch.contact == old:
                ch.contact = new
                touched += 1
        for g in self.rxgroups:
            g.contacts = [new if c == old else c for c in g.contacts]
            touched += sum(1 for c in g.contacts if c == new)
        return touched

    def sort_channels(self, key: str = "name") -> None:
        keys = {
            "name": lambda c: c.name.lower(),
            "rx": lambda c: c.rx_freq,
            "type": lambda c: (c.ch_type, c.name.lower()),
        }
        self.channels.sort(key=keys.get(key, keys["name"]))

    def renumber_zone_members(self) -> None:
        """Entfernt Zonen-Mitglieder, die es nicht (mehr) gibt."""
        names = {c.name for c in self.channels}
        for z in self.zones:
            z.channels = [c for c in z.channels if c in names]
            if z.a_channel not in names:
                z.a_channel = z.channels[0] if z.channels else ""
            if z.b_channel not in names:
                z.b_channel = z.channels[0] if z.channels else ""

    def stats(self) -> Dict[str, int]:
        return {
            "channels": len(self.channels),
            "talkgroups": len(self.talkgroups),
            "zones": len(self.zones),
            "scanlists": len(self.scanlists),
            "rxgroups": len(self.rxgroups),
            "radio_ids": len(self.radio_ids),
        }

    # -- Pruefen ---------------------------------------------------------
    def validate(self) -> List[Issue]:
        issues: List[Issue] = []
        add = issues.append

        limits = [("channels", MAX_CHANNELS), ("talkgroups", MAX_TALKGROUPS),
                  ("zones", MAX_ZONES), ("scanlists", MAX_SCANLISTS),
                  ("rxgroups", MAX_RXGROUPS), ("radio_ids", MAX_RADIO_IDS)]
        for attr, limit in limits:
            n = len(getattr(self, attr))
            if n > limit:
                add(Issue("error", attr, "%d Eintraege, das Geraet erlaubt %d" % (n, limit)))

        if not self.radio_ids:
            add(Issue("error", "radio_ids", "Mindestens eine DMR-ID (Radio ID) wird benoetigt"))
        for rid in self.radio_ids:
            if not (1 <= rid.radio_id <= 16_777_215):
                add(Issue("error", "radio_ids/%s" % rid.name,
                          "DMR-ID %s liegt ausserhalb 1..16777215" % rid.radio_id))

        seen: Dict[str, int] = {}
        tg_names = {t.name for t in self.talkgroups}
        rid_names = {r.name for r in self.radio_ids}
        zone_scan = {s.name for s in self.scanlists} | {"None", ""}
        rxg_names = {g.name for g in self.rxgroups} | {"None", ""}

        for idx, ch in enumerate(self.channels, 1):
            where = "channel %d (%s)" % (idx, ch.label())
            if not ch.name:
                add(Issue("error", where, "Kanalname fehlt"))
            elif len(ch.name) > NAME_LEN:
                add(Issue("warning", where, "Name laenger als %d Zeichen, wird gekuerzt" % NAME_LEN))
            low = ch.name.strip().lower()
            if low:
                if low in seen:
                    add(Issue("error", where, "Doppelter Kanalname (auch Kanal %d)" % seen[low]))
                seen[low] = idx
            if not in_band(ch.rx_freq, RX_BANDS):
                add(Issue("error", where, "RX %s liegt ausserhalb der Empfangsbereiche"
                          % fmt_freq(ch.rx_freq)))
            if ch.tx_freq and not in_band(ch.tx_freq, TX_BANDS):
                add(Issue("warning", where, "TX %s liegt ausserhalb der Sendebereiche"
                          % fmt_freq(ch.tx_freq)))
            if ch.ch_type not in CHANNEL_TYPES:
                add(Issue("error", where, "Unbekannter Kanaltyp %r" % ch.ch_type))
            if ch.power not in POWER_LEVELS:
                add(Issue("error", where, "Unbekannte Sendeleistung %r" % ch.power))
            if ch.bandwidth not in BANDWIDTHS:
                add(Issue("error", where, "Unbekannte Bandbreite %r" % ch.bandwidth))
            if ch.is_digital:
                if not 0 <= ch.color_code <= 15:
                    add(Issue("error", where, "Color Code %d ausserhalb 0..15" % ch.color_code))
                if ch.slot not in (1, 2):
                    add(Issue("error", where, "Zeitschlitz %s ist weder 1 noch 2" % ch.slot))
                if ch.contact and ch.contact not in tg_names:
                    add(Issue("error", where, "Kontakt %r fehlt in der Talkgroup-Liste" % ch.contact))
                if ch.radio_id and ch.radio_id not in rid_names:
                    add(Issue("error", where, "Radio ID %r fehlt in der Radio-ID-Liste" % ch.radio_id))
                if ch.bandwidth != "12.5K":
                    add(Issue("warning", where, "DMR benoetigt 12.5K Bandbreite"))
            else:
                for tone, label in ((ch.ctcss_decode, "CTCSS/DCS Decode"),
                                    (ch.ctcss_encode, "CTCSS/DCS Encode")):
                    if not tone_is_valid(tone):
                        add(Issue("warning", where, "%s %r ist kein Standardwert" % (label, tone)))
            if ch.scan_list not in zone_scan:
                add(Issue("warning", where, "Scanliste %r ist nicht definiert" % ch.scan_list))
            if ch.rx_group not in rxg_names and not re.fullmatch(r"#\d+", ch.rx_group or ""):
                add(Issue("warning", where, "RX-Gruppenliste %r ist nicht definiert" % ch.rx_group))

        chan_names = {c.name for c in self.channels}
        for z in self.zones:
            where = "zone %s" % (z.name or "?")
            if not z.name:
                add(Issue("error", "zones", "Zone ohne Namen"))
            if not z.channels:
                add(Issue("error", where, "Zone enthaelt keine Kanaele"))
            if len(z.channels) > MAX_ZONE_MEMBERS:
                add(Issue("error", where, "%d Kanaele, erlaubt sind %d"
                          % (len(z.channels), MAX_ZONE_MEMBERS)))
            for cname in z.channels:
                if cname not in chan_names:
                    add(Issue("error", where, "Kanal %r existiert nicht" % cname))
            for label, cname in (("A", z.a_channel), ("B", z.b_channel)):
                if cname and cname not in chan_names:
                    add(Issue("error", where, "%s-Kanal %r existiert nicht" % (label, cname)))

        for sl in self.scanlists:
            for cname in sl.channels:
                if cname not in chan_names:
                    add(Issue("error", "scanlist %s" % sl.name, "Kanal %r existiert nicht" % cname))
            if len(sl.channels) > MAX_SCAN_MEMBERS:
                add(Issue("warning", "scanlist %s" % sl.name,
                          "%d Kanaele, empfohlen sind max. %d" % (len(sl.channels), MAX_SCAN_MEMBERS)))

        for g in self.rxgroups:
            if len(g.contacts) > MAX_RXGROUP_MEMBERS:
                add(Issue("error", "rxgroup %s" % g.name,
                          "%d Kontakte, erlaubt sind %d" % (len(g.contacts), MAX_RXGROUP_MEMBERS)))
            for cname in g.contacts:
                if cname not in tg_names:
                    add(Issue("error", "rxgroup %s" % g.name, "Kontakt %r existiert nicht" % cname))

        tg_seen: Dict[int, str] = {}
        for tg in self.talkgroups:
            if tg.tg_id in tg_seen and tg.call_type == "Group Call":
                add(Issue("warning", "talkgroup %s" % tg.name,
                          "ID %d doppelt (auch %s)" % (tg.tg_id, tg_seen[tg.tg_id])))
            tg_seen[tg.tg_id] = tg.name
            if tg.call_type not in CALL_TYPES:
                add(Issue("error", "talkgroup %s" % tg.name, "Ruftyp %r unbekannt" % tg.call_type))
        return issues

    # -- Projektdatei ----------------------------------------------------
    def to_json(self, indent: int = 1) -> str:
        from .version import __version__
        payload = {
            "format": "atprog-codeplug",
            "format_version": 1,
            "app_version": __version__,
            "name": self.name,
            "model": self.model,
            "settings": self.settings,
            "radio_ids": [r.to_dict() for r in self.radio_ids],
            "talkgroups": [t.to_dict() for t in self.talkgroups],
            "channels": [c.to_dict() for c in self.channels],
            "zones": [z.to_dict() for z in self.zones],
            "scanlists": [s.to_dict() for s in self.scanlists],
            "rxgroups": [g.to_dict() for g in self.rxgroups],
        }
        return json.dumps(payload, indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Codeplug":
        data = json.loads(text)
        if data.get("format") != "atprog-codeplug":
            raise ValueError("Keine atprog-Projektdatei")
        cp = cls(name=data.get("name", "Codeplug"),
                 model=data.get("model", "AT-D878UV II Plus"),
                 settings=data.get("settings") or {})
        cp.radio_ids = [RadioID.from_dict(d) for d in data.get("radio_ids", [])]
        cp.talkgroups = [TalkGroup.from_dict(d) for d in data.get("talkgroups", [])]
        cp.channels = [Channel.from_dict(d) for d in data.get("channels", [])]
        cp.zones = [Zone.from_dict(d) for d in data.get("zones", [])]
        cp.scanlists = [ScanList.from_dict(d) for d in data.get("scanlists", [])]
        cp.rxgroups = [RxGroupList.from_dict(d) for d in data.get("rxgroups", [])]
        return cp

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Codeplug":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_json(fh.read())
