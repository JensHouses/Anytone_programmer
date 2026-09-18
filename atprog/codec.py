"""Generischer Binaer-Codec fuer die im Layout beschriebenen Datensaetze.

Wichtigste Eigenschaft: ``encode_record`` arbeitet immer *patchend*. Es bekommt
den Originaldatensatz aus dem Geraet und veraendert ausschliesslich die Bytes
der bekannten Felder. Unbekannte Bytes bleiben unberuehrt - dadurch koennen
Luecken im Layout keinen Schaden anrichten.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional

from .layout import Field, ObjectDef

INT_FMT = {"u8": "<B", "u16": "<H", "u32": "<I", "u16be": ">H", "u32be": ">I"}
MAX_LIST_ITEM = 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Bitmaps
# ---------------------------------------------------------------------------

def bitmap_get(data: bytes, index: int) -> bool:
    byte, bit = divmod(index, 8)
    return byte < len(data) and bool(data[byte] & (1 << bit))


def bitmap_set(data: bytearray, index: int, value: bool = True) -> None:
    byte, bit = divmod(index, 8)
    while len(data) <= byte:
        data.append(0)
    if value:
        data[byte] |= (1 << bit)
    else:
        data[byte] &= ~(1 << bit) & 0xFF


def bitmap_indices(data: bytes, limit: int, invert: bool = False) -> List[int]:
    """Belegte Indizes. ``invert=True``: geloeschtes Bit bedeutet belegt."""
    return [i for i in range(limit) if bitmap_get(data, i) != invert]


# ---------------------------------------------------------------------------
# Einzelfelder
# ---------------------------------------------------------------------------

def decode_str(raw: bytes) -> str:
    out = bytearray()
    for b in raw:
        if b in (0x00, 0xFF):
            break
        out.append(b)
    return out.decode("latin-1").rstrip()


def encode_str(text: str, length: int, pad: int = 0x00) -> bytes:
    raw = str(text or "").encode("latin-1", "replace")[:length]
    return raw + bytes([pad]) * (length - len(raw))


def decode_bcd_be(raw: bytes) -> int:
    value = 0
    for b in raw:
        hi, lo = b >> 4, b & 0x0F
        if hi > 9 or lo > 9:
            return 0
        value = value * 100 + hi * 10 + lo
    return value


def encode_bcd_be(value: int, length: int) -> bytes:
    digits = str(max(0, int(value))).rjust(length * 2, "0")[-length * 2:]
    return bytes(int(digits[i:i + 2], 16) for i in range(0, len(digits), 2))


def _get_int(data: bytes, field: Field) -> int:
    fmt = INT_FMT[field.type]
    return struct.unpack_from(fmt, data, field.offset)[0]


def _put_int(buf: bytearray, field: Field, value: int) -> None:
    fmt = INT_FMT[field.type]
    size = struct.calcsize(fmt)
    mask = (1 << (size * 8)) - 1
    struct.pack_into(fmt, buf, field.offset, int(value) & mask)


def decode_field(data: bytes, field: Field) -> Any:
    t = field.type
    if t in INT_FMT:
        value = _get_int(data, field) * (field.scale or 1)
        return _enum_out(field, value // (field.scale or 1)) if field.enum else value
    if t == "bits":
        mask = (1 << field.width) - 1
        value = (data[field.offset] >> field.shift) & mask
        return _enum_out(field, value)
    if t == "str":
        return decode_str(data[field.offset:field.offset + field.length])
    if t == "bcd_be":
        return decode_bcd_be(data[field.offset:field.offset + field.length]) * (field.scale or 1)
    if t == "index_list":
        item_size = struct.calcsize(INT_FMT[field.item])
        out: List[int] = []
        for i in range(field.count):
            off = field.offset + i * item_size
            if off + item_size > len(data):
                break
            (val,) = struct.unpack_from(INT_FMT[field.item], data, off)
            if val == field.empty:
                continue
            out.append(val)
        return out
    raise ValueError("Unbekannter Feldtyp %r" % t)


def encode_field(buf: bytearray, field: Field, value: Any) -> None:
    t = field.type
    if t in INT_FMT:
        raw = _enum_in(field, value) if field.enum else int(value) // (field.scale or 1)
        _put_int(buf, field, raw)
        return
    if t == "bits":
        raw = _enum_in(field, value) if field.enum else int(value)
        mask = (1 << field.width) - 1
        cur = buf[field.offset]
        buf[field.offset] = (cur & ~(mask << field.shift) & 0xFF) | ((int(raw) & mask) << field.shift)
        return
    if t == "str":
        # Das Geraet schreibt Name + Endezeichen und laesst den Rest des Feldes
        # unberuehrt (echte Datensaetze enthalten Reste frueherer Namen, etwa
        # b"OV LEV\x00 VFO B"). Genau so verfahren wir auch - sonst waere kein
        # Datensatz byteidentisch rekonstruierbar.
        raw = str(value or "").encode("latin-1", "replace")[:field.length]
        buf[field.offset:field.offset + len(raw)] = raw
        if len(raw) < field.length:
            buf[field.offset + len(raw)] = 0x00
        return
    if t == "bcd_be":
        raw = int(value or 0) // (field.scale or 1)
        buf[field.offset:field.offset + field.length] = encode_bcd_be(raw, field.length)
        return
    if t == "index_list":
        item_size = struct.calcsize(INT_FMT[field.item])
        items = list(value or [])[:field.count]
        for i in range(field.count):
            off = field.offset + i * item_size
            if off + item_size > len(buf):
                break
            raw = items[i] if i < len(items) else field.empty
            struct.pack_into(INT_FMT[field.item], buf, off, int(raw))
        return
    raise ValueError("Unbekannter Feldtyp %r" % t)


def _enum_out(field: Field, value: int) -> Any:
    names = field.enum or []
    return names[value] if 0 <= value < len(names) else value


def _enum_in(field: Field, value: Any) -> int:
    names = field.enum or []
    if isinstance(value, int):
        return value
    text = str(value)
    for i, name in enumerate(names):
        if name.lower() == text.lower():
            return i
    try:
        return int(text)
    except ValueError:
        raise ValueError("Wert %r ist fuer Feld %s nicht zulaessig (erlaubt: %s)"
                         % (value, field.name, ", ".join(names)))


# ---------------------------------------------------------------------------
# Datensaetze
# ---------------------------------------------------------------------------

def decode_record(obj: ObjectDef, data: bytes) -> Dict[str, Any]:
    if len(data) < obj.record_size:
        raise ValueError("%s: Datensatz zu kurz (%d < %d)" % (obj.key, len(data), obj.record_size))
    return {f.name: decode_field(data, f) for f in obj.fields}


def encode_record(obj: ObjectDef, values: Dict[str, Any],
                  original: Optional[bytes] = None) -> bytes:
    """Schreibt ``values`` in eine Kopie von ``original`` (Patch-Verfahren)."""
    if original is None:
        buf = bytearray(b"\x00" * obj.record_size)
    else:
        buf = bytearray(original[:obj.record_size])
        if len(buf) < obj.record_size:
            buf.extend(b"\x00" * (obj.record_size - len(buf)))
    for f in obj.fields:
        if f.name in values and values[f.name] is not None:
            encode_field(buf, f, values[f.name])
    return bytes(buf)


def roundtrip_ok(obj: ObjectDef, data: bytes) -> bool:
    """Prueft, ob Decodieren+Codieren den Datensatz unveraendert laesst."""
    return encode_record(obj, decode_record(obj, data), data) == bytes(data[:obj.record_size])
