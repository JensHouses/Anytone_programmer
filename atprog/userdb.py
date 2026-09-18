"""Digitale Kontaktliste (DMR-ID-Datenbank) des AT-D868/878.

Diese Datenbank liegt ausserhalb des Codeplugs und versorgt die Anzeige mit
Rufzeichen und Namen des gerade sendenden Gegenuebers.

Aufbau (an einem AT-D878UV II Plus geprueft, Adressen wie in qdmr):

    0x04000000  Index, Baenke zu 0x1F400 Byte im Abstand 0x40000
                je Eintrag 8 Byte: uint32 (BCD-ID << 1 | Gruppenflag),
                uint32 Byte-Offset des Datensatzes
    0x044C0000  Kopf: uint32 Anzahl, uint32 Ende der Datenbank
    0x04500000  Datensaetze, Baenke zu 0x186A0 Byte im Abstand 0x40000
                6 Byte Kopf (Ruftyp, ID, Flags), danach sechs mit 0x00
                abgeschlossene Zeichenketten: Name, Ort, Rufzeichen,
                Region, Land, Bemerkung

Der Index ist aufsteigend nach ID sortiert - das Geraet sucht binaer.
"""
from __future__ import annotations

import csv
import io
import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .image import MemoryImage

INDEX_ENTRY_SIZE = 8
HEADER_LEN = 6
MAX_ENTRIES = 500000        # AT-D878UV II Plus; der aeltere D868UV nimmt 200000


@dataclass(frozen=True)
class Geometry:
    """Ablage der Datenbank im Speicher - je Geraetetyp verschieden."""
    name: str
    index_base: int
    index_bank: int
    entry_base: int
    entry_bank: int
    bank_step: int
    limits: int

    def index_address(self, entry_no: int) -> int:
        bank, off = divmod(entry_no * INDEX_ENTRY_SIZE, self.index_bank)
        return self.index_base + bank * self.bank_step + off

    def entry_address(self, byte_off: int) -> int:
        bank, off = divmod(byte_off, self.entry_bank)
        return self.entry_base + bank * self.bank_step + off


# An einem AT-D878UV II Plus (Firmware V101) nachgemessen: Kopf bei 0x04840000,
# Datensaetze ab 0x05000000. Der aeltere D868UV nutzt 0x044C0000 / 0x04500000
# (so in qdmr beschrieben).
GEOMETRIES = {
    "d878uv2": Geometry("d878uv2", 0x04000000, 0x1F400, 0x05500000, 0x186A0,
                        0x40000, 0x04840000),
    "d868uv": Geometry("d868uv", 0x04000000, 0x1F400, 0x04500000, 0x186A0,
                       0x40000, 0x044C0000),
}
DEFAULT_GEOMETRY = GEOMETRIES["d878uv2"]

# Bequeme Kurznamen fuer die Vorgabegeometrie
INDEX_BASE = DEFAULT_GEOMETRY.index_base
INDEX_BANK_SIZE = DEFAULT_GEOMETRY.index_bank
BANK_STEP = DEFAULT_GEOMETRY.bank_step
LIMITS_ADDR = DEFAULT_GEOMETRY.limits
ENTRY_BASE = DEFAULT_GEOMETRY.entry_base
ENTRY_BANK_SIZE = DEFAULT_GEOMETRY.entry_bank

CALL_TYPES = ("Private Call", "Group Call", "All Call")
# Maximale Feldlaengen laut Geraetevorgabe
LIMITS = {"name": 16, "city": 15, "callsign": 8, "state": 16, "country": 16, "comment": 16}


class UserDbError(ValueError):
    pass


@dataclass
class UserEntry:
    """Ein Eintrag der Rufzeichendatenbank."""
    id: int = 0
    callsign: str = ""
    name: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    comment: str = ""
    call_type: int = 0          # 0 = Einzelruf
    flags: int = 0

    def label(self) -> str:
        parts = [self.callsign, self.name, self.city]
        return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Adressrechnung (Baenke)
# ---------------------------------------------------------------------------

def index_address(entry_no: int, geo: Geometry = DEFAULT_GEOMETRY) -> int:
    """Adresse des ``entry_no``-ten Indexeintrags."""
    return geo.index_address(entry_no)


def entry_address(byte_off: int, geo: Geometry = DEFAULT_GEOMETRY) -> int:
    """Adresse zu einem Byte-Offset im Datensatzstrom."""
    return geo.entry_address(byte_off)


def detect_geometry(read: Callable[[int, int], bytes],
                    samples: Sequence[Tuple[int, int]]) -> Optional[Tuple[Geometry, bool]]:
    """Ermittelt Geometrie und ID-Kodierung am angeschlossenen Geraet.

    ``read(addr, size)`` liest aus dem Geraet, ``samples`` sind Paare aus
    Indexschluessel und Datensatz-Offset. Es werden mehrere Stichproben
    genommen: einzelne Bereiche koennen geloescht sein, ohne dass die Datenbank
    als Ganzes unbrauchbar waere.
    """
    for geo in GEOMETRIES.values():
        hits = 0
        found: Optional[bool] = None
        for key, offset in samples:
            expected = key_to_id(key)
            if not expected:
                continue
            try:
                blob = read(geo.entry_address(offset), 192)
            except Exception:
                continue
            bcd = detect_id_encoding(blob, expected)
            if bcd is not None and (found is None or bcd == found):
                found, hits = bcd, hits + 1
        if found is not None and hits:
            return geo, found
    return None


def _write_stream(img: MemoryImage, base: int, bank_size: int, data: bytes,
                  step: int = BANK_STEP) -> None:
    """Schreibt einen fortlaufenden Strom in Baenke mit Luecken dazwischen."""
    pos = 0
    bank = 0
    while pos < len(data):
        chunk = data[pos:pos + bank_size]
        img.write(base + bank * step, chunk)
        pos += len(chunk)
        bank += 1


def _read_stream(img: MemoryImage, base: int, bank_size: int, length: int,
                 step: int = BANK_STEP) -> bytes:
    """Liest einen ueber Baenke verteilten Strom, so weit er vorhanden ist."""
    out = bytearray()
    bank = 0
    while len(out) < length:
        addr = base + bank * step
        want = min(bank_size, length - len(out), img.available(addr))
        if want <= 0:
            break
        out.extend(img.read(addr, want))
        bank += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# Kodierung
# ---------------------------------------------------------------------------

def bcd_from_int(value: int) -> int:
    """2635224 -> 0x02635224 (jede Dezimalstelle als Nibble)."""
    out = 0
    for shift, digit in enumerate(reversed(str(int(value))[:8])):
        out |= int(digit) << (4 * shift)
    return out


def int_from_bcd(value: int) -> Optional[int]:
    text = "%08X" % value
    return int(text) if all(c in "0123456789" for c in text) else None


def index_key(dmr_id: int, group: bool = False) -> int:
    return (bcd_from_int(dmr_id) << 1) | (1 if group else 0)


def key_to_id(key: int) -> Optional[int]:
    return int_from_bcd(key >> 1)


def bcd_bytes(value: int, length: int = 4) -> bytes:
    """ID als BCD in Big-Endian-Byte-Folge: 23401 -> 00 02 34 01."""
    digits = str(max(0, int(value)))[-length * 2:].rjust(length * 2, "0")
    return bytes(int(digits[i:i + 2], 16) for i in range(0, len(digits), 2))


def int_from_bcd_bytes(raw: bytes) -> Optional[int]:
    text = "".join("%02X" % b for b in raw)
    return int(text) if all(c in "0123456789" for c in text) else None


def _c_string(text: str, limit: int) -> bytes:
    raw = str(text or "").encode("latin-1", "replace")[:limit]
    return raw + b"\x00"


def encode_entry(entry: UserEntry, bcd_id: bool) -> bytes:
    """Baut einen Datensatz. ``bcd_id`` steuert die Kodierung der ID."""
    # Die ID steht als BCD in Big-Endian-Byte-Folge (am Geraet nachgemessen);
    # ``bcd_id=False`` erlaubt die alternative Little-Endian-Zahl.
    number = (bcd_bytes(entry.id) if bcd_id
              else struct.pack("<I", int(entry.id) & 0xFFFFFFFF))
    head = bytes([entry.call_type & 0xFF]) + number + bytes([entry.flags & 0xFF])
    body = b"".join([
        _c_string(entry.name, LIMITS["name"]),
        _c_string(entry.city, LIMITS["city"]),
        _c_string(entry.callsign, LIMITS["callsign"]),
        _c_string(entry.state, LIMITS["state"]),
        _c_string(entry.country, LIMITS["country"]),
        _c_string(entry.comment, LIMITS["comment"]),
    ])
    return head + body


def decode_entry(blob: bytes, offset: int, bcd_id: bool) -> Tuple[UserEntry, int]:
    """Liest einen Datensatz ab ``offset``; liefert Eintrag und neue Position."""
    if offset + HEADER_LEN > len(blob):
        raise UserDbError("Datensatz bei 0x%X reicht ueber das Ende hinaus" % offset)
    call_type = blob[offset]
    raw_id = blob[offset + 1:offset + 5]
    flags = blob[offset + 5]
    pos = offset + HEADER_LEN
    fields: List[str] = []
    for _ in range(6):
        end = blob.find(b"\x00", pos)
        if end < 0:
            raise UserDbError("Zeichenkette bei 0x%X ohne Abschluss" % pos)
        fields.append(blob[pos:end].decode("latin-1", "replace"))
        pos = end + 1
    dmr_id = (int_from_bcd_bytes(raw_id) if bcd_id
              else struct.unpack("<I", raw_id)[0])
    entry = UserEntry(id=dmr_id or 0, name=fields[0], city=fields[1], callsign=fields[2],
                      state=fields[3], country=fields[4], comment=fields[5],
                      call_type=call_type, flags=flags)
    return entry, pos


def detect_id_encoding(blob: bytes, expected_id: int) -> Optional[bool]:
    """Ermittelt anhand eines bekannten Datensatzes, ob die ID BCD ist."""
    for bcd in (True, False):
        try:
            entry, _ = decode_entry(blob, 0, bcd)
        except (UserDbError, struct.error):
            continue
        if entry.id == expected_id:
            return bcd
    return None


# ---------------------------------------------------------------------------
# Aufbau einer kompletten Datenbank
# ---------------------------------------------------------------------------

@dataclass
class UserDatabase:
    entries: List[UserEntry] = field(default_factory=list)
    bcd_id: bool = True
    geometry: Geometry = DEFAULT_GEOMETRY

    def sorted_entries(self) -> List[UserEntry]:
        return sorted(self.entries, key=lambda e: e.id)

    def build(self, progress: Optional[Callable[[int, int, str], None]] = None) -> MemoryImage:
        """Erzeugt das vollstaendige Speicherabbild der Datenbank."""
        entries = self.sorted_entries()
        if len(entries) > MAX_ENTRIES:
            raise UserDbError("%d Eintraege, das Geraet nimmt hoechstens %d"
                              % (len(entries), MAX_ENTRIES))
        index = bytearray()
        stream = bytearray()
        for n, entry in enumerate(entries):
            index.extend(struct.pack("<II", index_key(entry.id), len(stream)))
            stream.extend(encode_entry(entry, self.bcd_id))
            if progress and n % 2000 == 0:
                progress(n, len(entries), "Datenbank aufbauen")
        geo = self.geometry
        img = MemoryImage({"purpose": "userdb", "entries": len(entries),
                           "bytes": len(stream), "geometry": geo.name})
        _write_stream(img, geo.index_base, geo.index_bank, bytes(index), geo.bank_step)
        _write_stream(img, geo.entry_base, geo.entry_bank, bytes(stream), geo.bank_step)
        img.write(geo.limits,
                  struct.pack("<II", len(entries), geo.entry_address(len(stream)))
                  + b"\x00" * 8)
        if progress:
            progress(len(entries), len(entries), "Datenbank aufbauen")
        return img

    @classmethod
    def from_image(cls, img: MemoryImage, limit: int = 0,
                   geo: Geometry = DEFAULT_GEOMETRY) -> "UserDatabase":
        """Liest eine Datenbank aus einem Abbild (soweit enthalten)."""
        count, _end = read_limits(img, geo)
        if not count:
            raise UserDbError("Im Abbild steht keine Eintragsanzahl (0x044C0000 fehlt)")
        wanted = min(count, limit) if limit else count
        index = _read_stream(img, geo.index_base, geo.index_bank,
                             wanted * INDEX_ENTRY_SIZE, geo.bank_step)
        pairs = [struct.unpack_from("<II", index, i * INDEX_ENTRY_SIZE)
                 for i in range(len(index) // INDEX_ENTRY_SIZE)]
        if not pairs:
            raise UserDbError("Kein Index im Abbild enthalten")
        span = max(o for _, o in pairs) + 256
        stream = _read_stream(img, geo.entry_base, geo.entry_bank, span, geo.bank_step)
        first_id = key_to_id(pairs[0][0]) or 0
        bcd = detect_id_encoding(stream[pairs[0][1]:], first_id)
        if bcd is None:
            raise UserDbError("ID-Kodierung der Datensaetze nicht erkannt")
        db = cls(bcd_id=bcd, geometry=geo)
        for key, off in pairs:
            try:
                entry, _ = decode_entry(stream, off, bcd)
            except UserDbError:
                break
            entry.id = entry.id or (key_to_id(key) or 0)
            db.entries.append(entry)
        return db


EMPTY_KEY = 0xFFFFFFFF


def key_is_empty(key: int) -> bool:
    """Leerer Indexplatz (geloeschtes Flash) oder unbrauchbarer Schluessel."""
    return key in (EMPTY_KEY, 0) or key_to_id(key) is None


def count_by_index(read8: Callable[[int], bytes], maximum: int = MAX_ENTRIES,
                   geo: Geometry = DEFAULT_GEOMETRY) -> int:
    """Ermittelt die Eintragszahl allein aus dem Index.

    Der Index ist aufsteigend belegt und dahinter geloescht, also genuegt eine
    Binaersuche nach dem ersten leeren Platz. ``read8`` liefert acht Bytes zu
    einer Adresse - damit funktioniert das sowohl am Geraet als auch auf einem
    Abbild.
    """
    def used(n: int) -> bool:
        raw = read8(geo.index_address(n))
        if len(raw) < 8:
            return False
        key, _off = struct.unpack("<II", raw)
        return not key_is_empty(key)

    if not used(0):
        return 0
    low, high = 0, 1
    while high < maximum and used(high):
        low, high = high, min(high * 2, maximum)
    if high >= maximum and used(maximum - 1):
        return maximum
    while low + 1 < high:
        mid = (low + high) // 2
        if used(mid):
            low = mid
        else:
            high = mid
    return low + 1


def _written_so_far(radio, addr: int, data: bytes, known: int) -> int:
    """Wie weit der Block schon geschrieben ist - durch Zurueckelesen bestimmt."""
    probe = min(len(data), known + 4096)
    try:
        actual = radio.read_region(addr + known, probe - known)
    except Exception:
        return known
    same = 0
    for a, b in zip(actual, data[known:probe]):
        if a != b:
            break
        same += 1
    return known + same


def read_limits(img: MemoryImage, geo: Geometry = DEFAULT_GEOMETRY) -> Tuple[int, int]:
    """Anzahl und Endadresse; faellt auf die Binaersuche im Index zurueck."""
    count, end = 0, 0
    if img.has(geo.limits, 8):
        count, end = struct.unpack("<II", img.read(geo.limits, 8))
    if count in (0, EMPTY_KEY):        # Kopf geloescht oder nicht gelesen
        count = count_by_index(lambda a: img.read(a, 8) if img.has(a, 8) else b"", geo=geo)
        end = 0
    return count, end


# ---------------------------------------------------------------------------
# radioid.net-CSV
# ---------------------------------------------------------------------------

def read_user_csv(path: str, country: str = "", prefixes: Sequence[str] = (),
                  limit: int = 0) -> List[UserEntry]:
    """Liest eine Nutzerliste von radioid.net.

    ``country`` filtert auf das Land, ``prefixes`` auf ID-Anfaenge (z.B. "262").
    """
    entries: List[UserEntry] = []
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with io.open(path, "r", encoding=enc, newline="") as fh:
                text = fh.read()
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UserDbError("%s laesst sich in keiner bekannten Codierung lesen" % path)

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        data = {(k or "").strip().upper().replace(" ", "_"): (v or "").strip()
                for k, v in row.items()}
        raw_id = data.get("RADIO_ID") or data.get("ID") or data.get("DMR_ID") or ""
        if not raw_id.isdigit():
            continue
        dmr_id = int(raw_id)
        land = data.get("COUNTRY", "")
        if country and land.lower() != country.lower():
            continue
        if prefixes and not any(str(dmr_id).startswith(p) for p in prefixes):
            continue
        first = data.get("FIRST_NAME") or data.get("NAME") or ""
        surname = data.get("SURNAME") or data.get("LAST_NAME") or ""
        entries.append(UserEntry(
            id=dmr_id,
            callsign=data.get("CALLSIGN") or data.get("CALL") or "",
            name=(" ".join(p for p in (first, surname) if p)).strip() or first,
            city=data.get("CITY", ""), state=data.get("STATE", ""), country=land,
            comment=data.get("REMARKS", "")))
        if limit and len(entries) >= limit:
            break
    return entries


def write_user_csv(path: str, entries: Iterable[UserEntry]) -> int:
    columns = ["RADIO_ID", "CALLSIGN", "NAME", "CITY", "STATE", "COUNTRY", "REMARKS"]
    n = 0
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for e in entries:
            writer.writerow([e.id, e.callsign, e.name, e.city, e.state, e.country, e.comment])
            n += 1
    return n


def describe(db: "UserDatabase") -> str:
    entries = db.entries
    if not entries:
        return "Datenbank ist leer."
    countries: Dict[str, int] = {}
    for e in entries:
        countries[e.country or "?"] = countries.get(e.country or "?", 0) + 1
    top = sorted(countries.items(), key=lambda kv: -kv[1])[:5]
    return ("%d Eintraege, IDs %d .. %d, ID-Kodierung %s\nLaender: %s"
            % (len(entries), entries[0].id, entries[-1].id,
               "BCD" if db.bcd_id else "binaer",
               ", ".join("%s %d" % kv for kv in top)))


# ---------------------------------------------------------------------------
# Zugriff auf das angeschlossene Geraet
# ---------------------------------------------------------------------------

@dataclass
class DbStatus:
    """Was am Geraet ueber die Datenbank bekannt ist."""
    geometry: Geometry = DEFAULT_GEOMETRY
    count: int = 0
    bcd_id: Optional[bool] = None
    samples: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        return bool(self.count) and self.bcd_id is not None

    def summary(self) -> str:
        return ("%s: %d Eintraege, ID-Kodierung %s"
                % (self.geometry.name, self.count,
                   "BCD" if self.bcd_id else "binaer" if self.bcd_id is False
                   else "unbekannt"))


def probe(radio, geometry: str = "", log: Callable[[str], None] = lambda m: None) -> DbStatus:
    """Ermittelt Geometrie, Eintragszahl und ID-Kodierung am Geraet."""
    geo = GEOMETRIES.get(geometry or "", None)
    count = 0
    for candidate in ([geo] if geo else list(GEOMETRIES.values())):
        count, _end = struct.unpack("<II", radio.read_region(candidate.limits, 8))
        if count not in (0, EMPTY_KEY):
            geo = candidate
            break
    if not count or count == EMPTY_KEY:
        geo = geo or DEFAULT_GEOMETRY
        log("Kopfblock leer - Anzahl wird aus dem Index bestimmt")
        count = count_by_index(lambda a: radio.read_block(a, 8), geo=geo)

    positions = sorted({0, 1, count // 4, count // 2, (count * 3) // 4,
                        max(0, count - 2)}) if count else []
    samples: List[Tuple[int, int]] = []
    for n in positions:
        raw = radio.read_region(geo.index_address(n), INDEX_ENTRY_SIZE)
        key, offset = struct.unpack("<II", raw)
        if key not in (0, EMPTY_KEY):
            samples.append((key, offset))
    found = detect_geometry(lambda a, n: radio.read_region(a, n), samples)
    if found:
        geo, bcd = found
    else:
        bcd = None
    return DbStatus(geometry=geo, count=count, bcd_id=bcd, samples=samples)


def sample_entries(radio, status: DbStatus, how_many: int = 8) -> List[UserEntry]:
    """Liest gleichmaessig verteilte Beispieleintraege."""
    out: List[UserEntry] = []
    if not status.readable:
        return out
    geo = status.geometry
    step = max(1, status.count // max(1, how_many))
    for n in range(0, status.count, step):
        raw = radio.read_region(geo.index_address(n), INDEX_ENTRY_SIZE)
        key, off = struct.unpack("<II", raw)
        try:
            entry, _ = decode_entry(radio.read_region(geo.entry_address(off), 192), 0,
                                    bool(status.bcd_id))
        except Exception:
            continue
        entry.id = entry.id or (key_to_id(key) or 0)
        out.append(entry)
        if len(out) >= how_many:
            break
    return out


def read_from_radio(radio, status: DbStatus, limit: int = 0,
                    progress: Optional[Callable[[int, int, str], None]] = None
                    ) -> List[UserEntry]:
    """Liest die Datenbank; der Datenstrom wird bankweise am Stueck geholt."""
    geo = status.geometry
    wanted = min(status.count, limit) if limit else status.count
    index = bytearray()
    need = wanted * INDEX_ENTRY_SIZE
    # Fortschritt ueber beide Abschnitte hinweg durchzaehlen, damit Prozentwert
    # und Restzeit nicht bei den Datensaetzen wieder bei null anfangen. Die
    # Groesse der Datensaetze ist erst nach dem Index bekannt und wird bis
    # dahin mit dem Erfahrungswert von 60 Byte je Eintrag angesetzt.
    est_total = need + wanted * 60
    while len(index) < need:
        n = len(index) // INDEX_ENTRY_SIZE
        room = geo.index_bank - (n * INDEX_ENTRY_SIZE) % geo.index_bank
        chunk = min(0x1000, need - len(index), room)
        index.extend(radio.read_region(geo.index_address(n), chunk))
        if progress:
            progress(len(index), est_total, "Index")
    pairs = [struct.unpack_from("<II", bytes(index), i * INDEX_ENTRY_SIZE)
             for i in range(wanted)]
    if not pairs:
        return []
    span = max(off for _, off in pairs) + 256
    total = need + span
    stream = bytearray()
    while len(stream) < span:
        room = geo.entry_bank - len(stream) % geo.entry_bank
        chunk = min(0x2000, span - len(stream), room)
        stream.extend(radio.read_region(geo.entry_address(len(stream)), chunk))
        if progress:
            progress(need + len(stream), total, "Datensaetze")
    blob = bytes(stream)
    out: List[UserEntry] = []
    for key, off in pairs:
        try:
            entry, _ = decode_entry(blob, off, bool(status.bcd_id))
        except Exception:
            continue
        entry.id = entry.id or (key_to_id(key) or 0)
        out.append(entry)
    return out


def verify_on_radio(radio, status: DbStatus, samples: int = 40,
                    progress: Optional[Callable[[int, int, str], None]] = None
                    ) -> Dict[str, object]:
    """Prueft stichprobenweise, ob zu jedem Indexeintrag ein Datensatz passt.

    Die Stichproben liegen gleichmaessig ueber die gesamte Datenbank - so faellt
    auch ein abgebrochener Schreibvorgang auf, bei dem nur das Ende fehlt.
    """
    geo = status.geometry
    if not status.count:
        return {"checked": 0, "ok": 0, "bad": [], "first_bad": None}
    # Gleichmaessige Stichproben reichen nicht: faellt eine ganze Flash-Bank
    # aus, kann sie zwischen zwei Stichproben liegen. Deshalb zusaetzlich am
    # Anfang jeder Indexbank pruefen.
    step = max(1, status.count // max(1, samples))
    positions = set(range(0, status.count, step))
    je_bank = geo.index_bank // INDEX_ENTRY_SIZE
    positions.update(range(0, status.count, je_bank))
    positions.update(n + je_bank - 1 for n in range(0, status.count, je_bank)
                     if n + je_bank - 1 < status.count)
    positions.add(status.count - 1)
    positions = sorted(positions)
    def entry_ok(pos: int) -> bool:
        raw = radio.read_region(geo.index_address(pos), INDEX_ENTRY_SIZE)
        key, off = struct.unpack("<II", raw)
        expected = key_to_id(key)
        # Nicht ueber die Bankgrenze hinaus lesen: dahinter liegt eine Luecke,
        # deren Inhalt nichts mit dem Datenstrom zu tun hat.
        room = geo.entry_bank - (off % geo.entry_bank)
        blob = radio.read_region(geo.entry_address(off), min(192, room))
        if len(blob) < 192 and room < 192:      # Satz laeuft in die naechste Bank
            blob += radio.read_region(geo.entry_address(off + len(blob)), 192 - len(blob))
        try:
            entry, _ = decode_entry(blob, 0, bool(status.bcd_id))
        except Exception:
            return False
        return bool(expected) and entry.id == expected

    good, bad = 0, []
    for n, pos in enumerate(positions):
        if entry_ok(pos):
            good += 1
        else:
            bad.append(pos)
        if progress:
            progress(n + 1, len(positions) + 1, "Pruefe Eintrag %d" % pos)

    # Grenze einkreisen: zwischen letzter guter und erster schlechter Stichprobe
    boundary = None
    if bad:
        first_bad = bad[0]
        low = max([p for p in positions if p < first_bad and p not in bad] or [-1])
        high = first_bad
        while low + 1 < high:
            mid = (low + high) // 2
            if entry_ok(mid):
                low = mid
            else:
                high = mid
        boundary = high
    if progress:
        progress(len(positions) + 1, len(positions) + 1, "fertig")
    return {"checked": len(positions), "ok": good, "bad": bad,
            "first_bad": bad[0] if bad else None, "boundary": boundary}


def write_to_radio(radio, img: MemoryImage, status: DbStatus,
                   progress: Optional[Callable[[int, int, str], None]] = None,
                   log: Callable[[str], None] = lambda m: None,
                   accept_risk: bool = False) -> Dict[str, object]:
    """Uebertraegt eine fertige Datenbank und prueft sie stichprobenweise.

    Ist die neue Liste kuerzer als die bisherige, werden die ueberzaehligen
    Indexeintraege geloescht - sonst blieben Reste der alten Liste stehen.
    """
    geo = status.geometry
    new_count, _end = read_limits(img, geo)
    total = img.size()
    done = 0
    for addr, size in img.ranges():
        data = img.read(addr, size)
        written = 0
        attempts = 0
        while written < size:
            try:
                radio.write_region(addr + written, data[written:], progress=progress,
                                   label="Schreibe 0x%08X" % (addr + written),
                                   done=done + written, total=total)
                written = size
            except Exception as exc:
                attempts += 1
                if attempts > 5:
                    raise
                # Das Geraet antwortet gelegentlich nach laengerem Schreiben
                # nicht mehr. Programmiermodus neu aufbauen und weitermachen,
                # statt den ganzen Vorgang zu verwerfen.
                log("Unterbrechung bei 0x%08X (%s) - Verbindung wird erneuert"
                    % (addr + written, exc))
                moved = radio.resync_program_mode()
                if not moved:
                    raise
                written = max(written, _written_so_far(radio, addr, data, written))
        done += size

    stale = max(0, status.count - new_count)
    if stale:
        log("Loesche %d ueberzaehlige Indexeintraege" % stale)
        blank = b"\xff" * (INDEX_ENTRY_SIZE * 64)
        for n in range(new_count, status.count, 64):
            radio.write_region(geo.index_address(n),
                               blank[:INDEX_ENTRY_SIZE * min(64, status.count - n)])
            if progress:
                progress(n - new_count, stale, "Alten Index loeschen")

    # Erst die Sitzung beenden - das Geraet uebernimmt die Daten beim END -
    # und danach in einer neuen Sitzung pruefen.
    log("Beende die Schreibsitzung und warte auf den Neustart ...")
    radio.reopen()
    head_ok = radio.read_region(geo.limits, 8) == img.read(geo.limits, 8)
    checked: List[UserEntry] = []
    for n in sorted({0, new_count // 2, max(0, new_count - 1)}):
        raw = radio.read_region(geo.index_address(n), INDEX_ENTRY_SIZE)
        key, off = struct.unpack("<II", raw)
        try:
            entry, _ = decode_entry(radio.read_region(geo.entry_address(off), 192), 0, True)
        except Exception:
            continue
        if entry.id == (key_to_id(key) or 0):
            checked.append(entry)
    return {"count": new_count, "stale_cleared": stale, "header_ok": head_ok,
            "verified": len(checked), "samples": checked}


# ---------------------------------------------------------------------------
# Bezug der Liste von radioid.net
# ---------------------------------------------------------------------------

RADIOID_URL = "https://database.radioid.net/static/users.json"


def fetch_user_database(dest: str, url: str = RADIOID_URL,
                        progress: Optional[Callable[[int, int, str], None]] = None,
                        timeout: float = 60.0) -> int:
    """Laedt die Nutzerliste von radioid.net und legt sie unter ``dest`` ab.

    Die Datei ist mehrere zehn Megabyte gross und wird stueckweise geschrieben,
    damit sie nicht vollstaendig im Speicher liegen muss.
    """
    import urllib.request

    request = urllib.request.Request(url, headers={
        "User-Agent": "atprog/%s (AnyTone programmer)" % __import__("atprog").__version__,
        "Accept": "application/json",
    })
    written = 0
    tmp = dest + ".teil"
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as fh:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                if progress:
                    progress(written, total or written + 1, "Lade von radioid.net")
    os.replace(tmp, dest)
    if progress:
        progress(written, written, "Lade von radioid.net")
    return written


def iter_json_users(path: str) -> Iterable[UserEntry]:
    """Liest die JSON-Liste satzweise.

    Die Datei enthaelt hunderttausende Objekte; sie am Stueck zu laden kostet
    ein Vielfaches an Speicher. Deshalb wird der Text durchlaufen und jedes
    Nutzerobjekt einzeln ausgewertet. Erkannt werden beide Formen: ein
    Wurzelobjekt mit Liste (``{"users": [...]}``) und eine blanke Liste.
    """
    import json as _json

    depth = 0
    inner: Optional[int] = None      # Klammerebene, auf der die Nutzer liegen
    cur: Optional[List[str]] = None
    in_string = escape = False

    with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            for ch in block:
                if inner is None:                 # Wurzel bestimmen
                    if ch == "{":
                        inner, depth = 1, 1
                    elif ch == "[":
                        inner, depth = 0, 0
                    continue
                if in_string:
                    if cur is not None:
                        cur.append(ch)
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                    if cur is not None:
                        cur.append(ch)
                elif ch == "{":
                    depth += 1
                    if depth == inner + 1:
                        cur = ["{"]
                    elif cur is not None:
                        cur.append(ch)
                elif ch == "}":
                    if cur is not None:
                        cur.append(ch)
                    if depth == inner + 1 and cur is not None:
                        text, cur = "".join(cur), None
                        try:
                            obj = _json.loads(text)
                        except ValueError:
                            obj = None
                        if isinstance(obj, dict) and obj.get("id") is not None:
                            yield _entry_from_json(obj)
                    depth -= 1
                elif cur is not None:
                    cur.append(ch)


def _entry_from_json(obj: Dict[str, object]) -> UserEntry:
    def text(key: str) -> str:
        value = obj.get(key)
        return "" if value is None else str(value).strip()
    name = (" ".join(p for p in (text("fname"), text("surname")) if p)).strip()
    try:
        dmr_id = int(str(obj.get("id")).strip())
    except (TypeError, ValueError):
        dmr_id = 0
    return UserEntry(id=dmr_id, callsign=text("callsign"), name=name or text("fname"),
                     city=text("city"), state=text("state"), country=text("country"),
                     comment=text("remarks"))


def read_user_file(path: str, country: str = "", prefixes: Sequence[str] = (),
                   limit: int = 0) -> List[UserEntry]:
    """Liest eine Nutzerliste - CSV von radioid.net oder deren JSON-Abzug."""
    if os.path.splitext(path)[1].lower() != ".json":
        return read_user_csv(path, country=country, prefixes=prefixes, limit=limit)
    out: List[UserEntry] = []
    for entry in iter_json_users(path):
        if not entry.id:
            continue
        if country and entry.country.lower() != country.lower():
            continue
        if prefixes and not any(str(entry.id).startswith(p) for p in prefixes):
            continue
        out.append(entry)
        if limit and len(out) >= limit:
            break
    return out
