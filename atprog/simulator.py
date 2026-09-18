"""Geraetesimulator fuer Tests ohne Funkgeraet.

Startet einen virtuellen seriellen Port (pty) und beantwortet das komplette
AnyTone-Programmierprotokoll aus einem RAM-Speicher. Damit laesst sich der
gesamte Lese-, Schreib- und Pruefweg ohne Hardware nachvollziehen.
"""
from __future__ import annotations

import errno
import os
import select
import struct
import threading
import time
from typing import Dict, Optional

from .protocol import ACK, CMD_END, CMD_PROGRAM, checksum


class SimulatedRadio:
    def __init__(self, model: str = "878UV2", version: str = "V400", page: int = 4096):
        self.model = model
        self.version = version
        self.page = page
        self.mem: Dict[int, bytearray] = {}
        self._master: Optional[int] = None
        self._slave: Optional[int] = None
        self.slave_name: str = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.in_program_mode = False
        self.writes = 0
        self.reads = 0

    # -- Speicher ---------------------------------------------------------
    def _page_of(self, addr: int) -> bytearray:
        base = addr - (addr % self.page)
        if base not in self.mem:
            self.mem[base] = bytearray(b"\xff" * self.page)
        return self.mem[base]

    def poke(self, addr: int, data: bytes) -> None:
        for i, byte in enumerate(data):
            here = addr + i
            self._page_of(here)[here % self.page] = byte

    def peek(self, addr: int, size: int) -> bytes:
        return bytes(self._page_of(addr + i)[(addr + i) % self.page] for i in range(size))

    # -- Port -------------------------------------------------------------
    def start(self) -> str:
        master, slave = os.openpty()
        self._master = master
        # Der Slave-Deskriptor bleibt offen, sonst liefert der Master EOF,
        # sobald das Programm den Port kurzzeitig schliesst.
        self._slave = slave
        self.slave_name = os.ttyname(slave)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.slave_name

    def stop(self) -> None:
        self._stop.set()
        for attr in ("_master", "_slave"):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)
        if self._thread:
            self._thread.join(timeout=1.0)

    def __enter__(self) -> "SimulatedRadio":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- Protokoll --------------------------------------------------------
    def _read(self, n: int, timeout: float = 5.0) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while len(buf) < n and not self._stop.is_set():
            if time.monotonic() > deadline:
                break
            try:
                rfds, _, _ = select.select([self._master], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not rfds:
                continue
            try:
                chunk = os.read(self._master, n - len(buf))
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EAGAIN, errno.EWOULDBLOCK):
                    continue          # kein Client verbunden - weiter warten
                break
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def _write(self, data: bytes) -> None:
        try:
            os.write(self._master, data)
        except OSError:
            pass

    def _serve(self) -> None:
        """Kommandos einsammeln und beantworten.

        'PROGRAM' und 'END' beginnen mit Zeichen, die selbst keine Kommandos
        sind - deshalb wird zuerst auf diese Praefixe geprueft und erst danach
        auf die Ein-Byte-Kommandos R, W, 0x02 und ACK.
        """
        pending = bytearray()
        while not self._stop.is_set():
            chunk = self._read(1, timeout=3600.0)
            if not chunk:
                continue
            pending.extend(chunk)
            data = bytes(pending)

            if CMD_PROGRAM.startswith(data):
                if data == CMD_PROGRAM:
                    pending.clear()
                    self.in_program_mode = True
                    self._write(b"QX" + bytes([ACK]))
                continue
            if CMD_END.startswith(data):
                if data == CMD_END:
                    pending.clear()
                    self.in_program_mode = False
                    self._write(bytes([ACK]))
                continue

            cmd = data[0]
            if cmd == 0x02:
                pending.clear()
                ident = (self.model.encode("ascii")[:7].ljust(7, b"\x00")
                         + self.version.encode("ascii")[:8].ljust(8, b"\x00")
                         + bytes([ACK]))
                self._write(ident[:16])
                continue
            if cmd == ACK:
                pending.clear()
                self._write(bytes([ACK]))
                continue
            if cmd == ord("R"):
                header = self._collect(pending, 6)
                if header is None:
                    continue
                addr, size = struct.unpack(">IB", header[1:6])
                self.reads += 1
                payload = self.peek(addr, size)
                head = struct.pack(">IB", addr, size)
                self._write(b"W" + head + payload
                            + bytes([checksum(head + payload), ACK]))
                continue
            if cmd == ord("W"):
                header = self._collect(pending, 6)
                if header is None:
                    continue
                addr, size = struct.unpack(">IB", header[1:6])
                body = self._collect(pending, size + 2)
                if body is None:
                    continue
                payload, csum = body[:size], body[size]
                head = struct.pack(">IB", addr, size)
                if csum != checksum(head + payload):
                    self._write(b"\x15")                 # NAK
                    continue
                self.poke(addr, payload)
                self.writes += 1
                self._write(bytes([ACK]))
                continue
            pending.clear()                              # unbekanntes Byte verwerfen

    def _collect(self, pending: bytearray, size: int) -> Optional[bytes]:
        """Ergaenzt ``pending`` auf ``size`` Bytes und entnimmt sie."""
        while len(pending) < size and not self._stop.is_set():
            more = self._read(size - len(pending), timeout=5.0)
            if not more:
                pending.clear()
                return None
            pending.extend(more)
        out = bytes(pending[:size])
        del pending[:size]
        return out


def populate_demo(sim: SimulatedRadio, channels: int = 8) -> None:
    """Legt eine kleine, plausible Beispielbelegung im Simulator an."""
    from . import layout as layout_mod
    from .codec import bitmap_set, encode_record

    lay = layout_mod.load()
    ch_obj, zn_obj, zl_obj = lay.obj("channel"), lay.obj("zone_name"), lay.obj("zone_list")
    ct_obj, rid_obj = lay.obj("contact"), lay.obj("radio_id")

    ch_bits, zn_bits, ct_bits = bytearray(), bytearray(), bytearray()
    for i in range(channels):
        rx = 438_000_000 + i * 12_500
        values = {"rx_freq": rx, "tx_offset": 7_600_000, "repeater": "Minus",
                  "ch_type": "D-Digital" if i % 2 else "A-Analog",
                  "power": "High", "bandwidth": "12.5K", "ptt_prohibit": "Off",
                  "call_confirmation": "Off", "talkaround": "Off",
                  "name": "DEMO %02d" % (i + 1)}
        sim.poke(ch_obj.address(i), encode_record(ch_obj, values, b"\x00" * ch_obj.record_size))
        bitmap_set(ch_bits, i, True)

    for i, (name, tg) in enumerate([("Ortsrunde", 8), ("Welt", 91), ("DL", 262)]):
        rec = encode_record(ct_obj, {"name": name, "tg_id": tg, "call_type": "Group Call",
                                     "call_alert": "None"}, b"\x00" * ct_obj.record_size)
        sim.poke(ct_obj.address(i), rec)
        bitmap_set(ct_bits, i, True)

    sim.poke(rid_obj.address(0), encode_record(
        rid_obj, {"radio_id": 2621234, "name": "DL0DEMO"}, b"\x00" * rid_obj.record_size))

    sim.poke(zn_obj.address(0), encode_record(
        zn_obj, {"name": "Demo-Zone"}, b"\x00" * zn_obj.record_size))
    sim.poke(zl_obj.address(0), encode_record(
        zl_obj, {"members": list(range(channels))}, b"\xff" * zl_obj.record_size))
    bitmap_set(zn_bits, 0, True)

    # Bitmaps immer in voller Bereichsgroesse schreiben - sonst bleiben die
    # restlichen Bytes auf dem Loeschwert 0xFF stehen und wuerden falsch
    # gedeutet. Das Kontakt-Bitmap des Geraets ist invertiert (0 = belegt).
    for region_name, bits in (("channel_bitmap", ch_bits), ("zone_bitmap", zn_bits),
                              ("scanlist_bitmap", bytearray()), ("radioid_bitmap", bytearray([1])),
                              ("contact_bitmap", ct_bits)):
        region = lay.region(region_name)
        if region.invert:
            blob = bytearray(b"\xff" * region.size)
            for i in range(len(bits) * 8):
                if bits[i // 8] & (1 << (i % 8)):
                    blob[i // 8] &= ~(1 << (i % 8)) & 0xFF
            sim.poke(region.addr, bytes(blob))
        else:
            sim.poke(region.addr, bytes(bits).ljust(region.size, b"\x00"))
