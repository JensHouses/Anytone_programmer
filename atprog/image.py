"""Sparse-Speicherabbild des Geraets (Backup-/Restore-Container).

Der Adressraum des AT-D878UV II Plus ist gross und weitgehend leer. Gelesen
werden nur die tatsaechlich belegten Bereiche; dieses Modul haelt sie als
Liste von Segmenten und speichert sie in einer Containerdatei (.atbin).
"""
from __future__ import annotations

import json
import struct
from typing import Dict, Iterable, List, Optional, Tuple

# Kennung des Dateiformats - eine Formatmarke, keine Programmversion.
# Sie bleibt unveraendert, damit bereits erstellte Abbilder lesbar bleiben.
MAGIC = b"ATPROG\x04\x00"
CONTAINER_VERSION = 1


class ImageError(ValueError):
    pass


class MemoryImage:
    """Adressierbarer, luecken-toleranter Speicher."""

    def __init__(self, meta: Optional[Dict] = None):
        self._segs: List[List] = []        # [[addr, bytearray], ...] aufsteigend, disjunkt
        self.meta: Dict = dict(meta or {})

    # -- Basisoperationen ------------------------------------------------
    def write(self, addr: int, data: bytes) -> None:
        if not data:
            return
        data = bytearray(data)
        end = addr + len(data)
        merged: List[List] = []
        new_addr, new_buf = addr, data
        for seg_addr, seg_buf in self._segs:
            seg_end = seg_addr + len(seg_buf)
            if seg_end < new_addr or seg_addr > end:
                merged.append([seg_addr, seg_buf])
                continue
            start = min(seg_addr, new_addr)
            stop = max(seg_end, new_addr + len(new_buf))
            buf = bytearray(b"\xff" * (stop - start))
            buf[seg_addr - start:seg_addr - start + len(seg_buf)] = seg_buf
            buf[new_addr - start:new_addr - start + len(new_buf)] = new_buf
            new_addr, new_buf = start, buf
        merged.append([new_addr, new_buf])
        merged.sort(key=lambda s: s[0])
        self._segs = merged

    def read(self, addr: int, size: int, fill: Optional[int] = None) -> bytes:
        out = bytearray()
        pos = addr
        while pos < addr + size:
            seg = self._find(pos)
            if seg is None:
                if fill is None:
                    raise ImageError("Adresse 0x%08X ist im Abbild nicht enthalten" % pos)
                nxt = self._next_start(pos)
                gap = min(addr + size, nxt) - pos if nxt else addr + size - pos
                out.extend(bytes([fill]) * gap)
                pos += gap
                continue
            seg_addr, seg_buf = seg
            off = pos - seg_addr
            take = min(len(seg_buf) - off, addr + size - pos)
            out.extend(seg_buf[off:off + take])
            pos += take
        return bytes(out)

    def available(self, addr: int) -> int:
        """Wie viele zusammenhaengende Bytes ab ``addr`` im Abbild stehen."""
        seg = self._find(addr)
        if seg is None:
            return 0
        seg_addr, seg_buf = seg
        return seg_addr + len(seg_buf) - addr

    def has(self, addr: int, size: int) -> bool:
        try:
            self.read(addr, size)
            return True
        except ImageError:
            return False

    def _find(self, addr: int) -> Optional[List]:
        for seg_addr, seg_buf in self._segs:
            if seg_addr <= addr < seg_addr + len(seg_buf):
                return [seg_addr, seg_buf]
        return None

    def _next_start(self, addr: int) -> Optional[int]:
        for seg_addr, _ in self._segs:
            if seg_addr > addr:
                return seg_addr
        return None

    # -- Auskunft ---------------------------------------------------------
    def ranges(self) -> List[Tuple[int, int]]:
        return [(a, len(b)) for a, b in self._segs]

    def size(self) -> int:
        return sum(len(b) for _, b in self._segs)

    def __len__(self) -> int:
        return self.size()

    def __bool__(self) -> bool:
        return bool(self._segs)

    def copy(self) -> "MemoryImage":
        clone = MemoryImage(self.meta)
        clone._segs = [[a, bytearray(b)] for a, b in self._segs]
        return clone

    # -- Vergleich --------------------------------------------------------
    def diff(self, other: "MemoryImage", granularity: int = 1) -> List[Tuple[int, bytes, bytes]]:
        """Liefert zusammenhaengende Unterschiede als (Adresse, alt, neu)."""
        runs: List[Tuple[int, bytes, bytes]] = []
        for addr, buf in other._segs:
            if not self.has(addr, len(buf)):
                runs.append((addr, b"", bytes(buf)))
                continue
            mine = self.read(addr, len(buf))
            start = None
            for i in range(len(buf)):
                if mine[i] != buf[i]:
                    if start is None:
                        start = i
                elif start is not None:
                    if i - start >= granularity:
                        runs.append((addr + start, mine[start:i], bytes(buf[start:i])))
                    start = None
            if start is not None:
                runs.append((addr + start, mine[start:], bytes(buf[start:])))
        return runs

    def changed_blocks(self, other: "MemoryImage", block: int = 64) -> List[Tuple[int, bytes]]:
        """Bloecke aus ``other``, die sich von diesem Abbild unterscheiden.

        Grundlage des differenziellen Schreibens: unveraenderte Bereiche werden
        nie angefasst.
        """
        out: List[Tuple[int, bytes]] = []
        for addr, buf in other._segs:
            for off in range(0, len(buf), block):
                chunk = bytes(buf[off:off + block])
                here = addr + off
                if not self.has(here, len(chunk)) or self.read(here, len(chunk)) != chunk:
                    out.append((here, chunk))
        return out

    # -- Dateien ----------------------------------------------------------
    def save(self, path: str) -> None:
        meta = json.dumps(self.meta, ensure_ascii=False).encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(MAGIC)
            fh.write(struct.pack("<HI", CONTAINER_VERSION, len(meta)))
            fh.write(meta)
            fh.write(struct.pack("<I", len(self._segs)))
            for addr, buf in self._segs:
                fh.write(struct.pack("<II", addr, len(buf)))
            for _, buf in self._segs:
                fh.write(buf)

    @classmethod
    def load(cls, path: str) -> "MemoryImage":
        with open(path, "rb") as fh:
            blob = fh.read()
        if not blob.startswith(MAGIC):
            raise ImageError("%s ist keine atprog-Abbilddatei" % path)
        pos = len(MAGIC)
        version, meta_len = struct.unpack_from("<HI", blob, pos)
        pos += 6
        if version > CONTAINER_VERSION:
            raise ImageError("Abbildformat Version %d wird nicht unterstuetzt" % version)
        meta = json.loads(blob[pos:pos + meta_len].decode("utf-8") or "{}")
        pos += meta_len
        (count,) = struct.unpack_from("<I", blob, pos)
        pos += 4
        table = []
        for _ in range(count):
            addr, length = struct.unpack_from("<II", blob, pos)
            pos += 8
            table.append((addr, length))
        img = cls(meta)
        for addr, length in table:
            img._segs.append([addr, bytearray(blob[pos:pos + length])])
            pos += length
        img._segs.sort(key=lambda s: s[0])
        return img

    def export_raw(self, path: str, addr: int, size: int, fill: int = 0xFF) -> None:
        with open(path, "wb") as fh:
            fh.write(self.read(addr, size, fill=fill))

    @classmethod
    def from_raw(cls, path: str, addr: int = 0) -> "MemoryImage":
        img = cls({"source": "raw", "base": addr})
        with open(path, "rb") as fh:
            img.write(addr, fh.read())
        return img

    def hexdump(self, addr: int, size: int = 256, width: int = 16) -> str:
        data = self.read(addr, size, fill=0xFF)
        lines = []
        for off in range(0, len(data), width):
            row = data[off:off + width]
            hexa = " ".join("%02X" % b for b in row)
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            lines.append("%08X  %-*s  |%s|" % (addr + off, width * 3 - 1, hexa, text))
        return "\n".join(lines)
