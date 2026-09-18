"""USB-Protokoll der AnyTone AT-D868/878-Familie (Programmiermodus).

Ablauf:
    PC -> "PROGRAM"                        Radio -> "QX" 0x06
    PC -> 0x02                             Radio -> 16 Byte Kennung, endet 0x06
    PC -> 0x06                             Radio -> 0x06            (Quittung)
    PC -> "R" addr(4, big endian) len(1)   Radio -> "W" addr len data cksum 0x06
    PC -> "W" addr len data cksum 0x06     Radio -> 0x06
    PC -> "END"                            Radio -> 0x06  (Geraet startet neu)

Die Pruefsumme ist die Summe aller Bytes ab Adresse bis einschliesslich der
Nutzdaten, modulo 256.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .serialport import SerialError, SerialPort

ACK = 0x06
CMD_PROGRAM = b"PROGRAM"
CMD_IDENTIFY = b"\x02"
CMD_END = b"END"

DEFAULT_BLOCK = 64        # Bytes je Lesekommando (Maximum des Geraets)
DEFAULT_WRITE_BLOCK = 16  # Bytes je Schreibkommando - mehr nimmt das Geraet nicht an
MAX_BLOCK = 64

ProgressFn = Callable[[int, int, str], None]     # (erledigt, gesamt, text)


class ProtocolError(IOError):
    """Das Funkgeraet hat unerwartet oder gar nicht geantwortet."""


@dataclass
class RadioInfo:
    model: str
    version: str
    raw: bytes

    def __str__(self) -> str:
        return "%s (Firmware %s)" % (self.model or "?", self.version or "?")

    @property
    def is_878uv2(self) -> bool:
        m = normalise_model(self.model)
        return m.startswith("878UV2") or m.startswith("878UVII")


def normalise_model(model: str) -> str:
    """Vereinheitlicht Kennungen wie 'ID878UV2', 'D878UV II', '878UV2'."""
    text = (model or "").upper()
    for ch in "- _.":
        text = text.replace(ch, "")
    for prefix in ("ID", "D"):
        if text.startswith(prefix) and text[len(prefix):len(prefix) + 3].isdigit():
            text = text[len(prefix):]
            break
    return text


def checksum(payload: bytes) -> int:
    return sum(payload) & 0xFF


class Radio:
    """Kommunikation mit dem Funkgeraet im Programmiermodus."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0,
                 block_size: int = DEFAULT_BLOCK, retries: int = 3,
                 log: Optional[Callable[[str], None]] = None,
                 write_block_size: int = DEFAULT_WRITE_BLOCK,
                 ack_after_identify: bool = False,
                 write_delay: float = 0.0):
        if not 1 <= block_size <= MAX_BLOCK:
            raise ValueError("block_size muss zwischen 1 und %d liegen" % MAX_BLOCK)
        if not 1 <= write_block_size <= MAX_BLOCK:
            raise ValueError("write_block_size muss zwischen 1 und %d liegen" % MAX_BLOCK)
        self.port_name = port
        self.block_size = block_size
        # Lesen vertraegt 64 Byte, Schreiben nicht: die Geraete der Familie
        # quittieren groessere Schreibbloecke nicht (so auch in qdmr).
        self.write_block_size = write_block_size
        # qdmr quittiert die Kennung NICHT. Tut man es doch, schickt das Geraet
        # einen zweiten Kennungsblock nach - und Host und Geraet sind fortan um
        # eine Antwort versetzt. Lesen verzeiht das, Schreiben moeglicherweise
        # nicht. Vorgabe daher: kein zusaetzliches ACK.
        self.ack_after_identify = ack_after_identify
        # Pause nach jedem Schreibkommando. Das Geraet quittiert sofort - die
        # Quittung kommt offenbar von der Schnittstelle, nicht vom Flash.
        self.write_delay = write_delay
        self.retries = max(1, retries)
        self._log = log or (lambda msg: None)
        self._baudrate = baudrate
        self._timeout = timeout
        self._port = SerialPort(port, baudrate, timeout)
        self._in_program_mode = False
        self.info: Optional[RadioInfo] = None

    # -- Verbindung ------------------------------------------------------
    def open(self) -> "RadioInfo":
        self._port.open()
        self._port.reset_input()
        self.enter_program_mode()
        self.info = self.identify()
        self._log("Verbunden: %s" % self.info)
        return self.info

    def close(self, reboot: bool = True) -> None:
        try:
            if self._in_program_mode:
                self.leave_program_mode(reboot=reboot)
        except Exception as exc:          # Verbindung trotzdem sauber schliessen
            self._log("Hinweis beim Beenden: %s" % exc)
        finally:
            self._port.close()

    def reopen(self, wait: float = 25.0) -> "RadioInfo":
        """Sitzung beenden und neu aufbauen.

        Wichtig fuer Schreibvorgaenge: Das Geraet puffert geschriebene Daten
        und uebernimmt sie erst mit END. Wer in derselben Sitzung zurueckliest,
        sieht den alten Flash-Inhalt - und verwirft dabei offenbar den Puffer.
        Also: schreiben, Sitzung beenden, Neustart abwarten, neu verbinden und
        erst dann pruefen.
        """
        self.close(reboot=True)
        time.sleep(2.5)                      # Geraet startet neu
        deadline = time.monotonic() + wait
        letzter: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                self._port = SerialPort(self.port_name, self._baudrate, self._timeout)
                return self.open()
            except Exception as exc:
                letzter = exc
                time.sleep(1.0)
        raise ProtocolError("Geraet meldet sich nach dem Neustart nicht wieder (%s)" % letzter)

    def __enter__(self) -> "Radio":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close(reboot=True)

    # -- Handshake -------------------------------------------------------
    def enter_program_mode(self) -> None:
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                self._port.reset_input()
                self._port.write(CMD_PROGRAM)
                reply = self._port.read(3, timeout=2.0)
                if reply[:2] == b"QX":
                    self._in_program_mode = True
                    return
                if reply and reply[-1] == ACK:
                    self._in_program_mode = True
                    return
                last = ProtocolError("Unerwartete Antwort auf PROGRAM: %r" % reply)
            except SerialError as exc:
                last = exc
            time.sleep(0.3)
        raise ProtocolError(
            "Funkgeraet antwortet nicht im Programmiermodus (%s). Pruefen: Geraet "
            "eingeschaltet, USB-Kabel mit Datenleitungen, richtiger Port. (%s)"
            % (self.port_name, last))

    def identify(self) -> RadioInfo:
        self._port.write(CMD_IDENTIFY)
        raw = self._port.read(16, timeout=2.0)
        if len(raw) < 8:
            raise ProtocolError("Kennung unvollstaendig empfangen: %r" % raw)
        body = raw[:-1] if raw[-1] == ACK else raw
        # Die Kennung besteht aus lesbaren Feldern, getrennt durch Nullbytes:
        # z.B. b"ID878UV2\x00V400\x00..." - Laenge und Anzahl sind nicht fest.
        parts = [p.decode("ascii", "replace").strip()
                 for p in body.split(b"\x00") if p.strip(b"\xff\x20")]
        model = parts[0] if parts else ""
        version = parts[1] if len(parts) > 1 else ""
        if self.ack_after_identify:
            try:
                self._port.write(bytes([ACK]))
                self._port.read(1, timeout=1.0)
            except SerialError:
                pass
        self.drain()
        return RadioInfo(model=model, version=version, raw=raw)

    def drain(self, timeout: float = 0.2) -> bytes:
        """Restbytes aus dem Eingangspuffer entfernen.

        Nach dem Handschlag liefern manche Firmwares noch Bytes nach (z.B.
        0xC0). Ohne Leeren laeuft das erste Lesekommando darauf auf und muss
        wiederholt werden.
        """
        leftovers = bytearray()
        while True:
            try:
                chunk = self._port.read(64, timeout=timeout)
            except SerialError:
                break
            if not chunk:
                break
            leftovers.extend(chunk)
        if leftovers:
            self._log("Puffer bereinigt: %d Byte verworfen (%s)"
                      % (len(leftovers), leftovers[:8].hex(" ")))
        return bytes(leftovers)

    def leave_program_mode(self, reboot: bool = True) -> None:
        self._port.write(CMD_END)
        self._port.read(1, timeout=2.0)
        self._in_program_mode = False
        if reboot:
            time.sleep(0.2)

    # -- Bloecke ---------------------------------------------------------
    def read_block(self, addr: int, size: int) -> bytes:
        if not 1 <= size <= MAX_BLOCK:
            raise ValueError("Blockgroesse %d unzulaessig" % size)
        cmd = b"R" + struct.pack(">IB", addr, size)
        last: Optional[Exception] = None
        for _ in range(self.retries):
            try:
                self._port.write(cmd)
                head = self._port.read_exact(6)
                if head[0:1] != b"W":
                    raise ProtocolError("Antwort beginnt mit %r statt 'W' @0x%08X" % (head[0:1], addr))
                raddr, rlen = struct.unpack(">IB", head[1:6])
                body = self._port.read_exact(rlen + 2)
                data, csum, ack = body[:rlen], body[rlen], body[rlen + 1]
                if raddr != addr:
                    raise ProtocolError("Adresse 0x%08X erwartet, 0x%08X erhalten" % (addr, raddr))
                if ack != ACK:
                    raise ProtocolError("Fehlende Quittung beim Lesen von 0x%08X" % addr)
                if csum != checksum(head[1:6] + data):
                    raise ProtocolError("Pruefsummenfehler beim Lesen von 0x%08X" % addr)
                return data
            except (ProtocolError, SerialError) as exc:
                last = exc
                self._log("Wiederhole Lesen 0x%08X: %s" % (addr, exc))
                self._resync()
        raise ProtocolError("Lesen bei 0x%08X endgueltig fehlgeschlagen: %s" % (addr, last))

    def write_block(self, addr: int, data: bytes) -> None:
        if not 1 <= len(data) <= MAX_BLOCK:
            raise ValueError("Blockgroesse %d unzulaessig" % len(data))
        head = struct.pack(">IB", addr, len(data))
        frame = b"W" + head + data + bytes([checksum(head + data), ACK])
        last: Optional[Exception] = None
        for _ in range(self.retries):
            try:
                self._port.write(frame)
                reply = self._port.read_exact(1)
                if reply[0] != ACK:
                    raise ProtocolError("Geraet lehnt Schreiben bei 0x%08X ab (0x%02X)"
                                        % (addr, reply[0]))
                if self.write_delay:
                    time.sleep(self.write_delay)
                return
            except (ProtocolError, SerialError) as exc:
                last = exc
                self._log("Wiederhole Schreiben 0x%08X: %s" % (addr, exc))
                self._resync()
        raise ProtocolError("Schreiben bei 0x%08X endgueltig fehlgeschlagen: %s" % (addr, last))

    def resync_program_mode(self) -> bool:
        """Programmiermodus neu aufbauen, ohne den Port zu schliessen.

        Nach langen Schreibvorgaengen antwortet das Geraet gelegentlich nicht
        mehr; ein neuer Handschlag bringt es meist zurueck.
        """
        time.sleep(1.0)
        try:
            self._port.reset_input()
            self._port.write(CMD_END)
            self._port.read(1, timeout=1.0)
        except SerialError:
            pass
        time.sleep(1.5)
        for _ in range(5):
            try:
                self._port.reset_input()
                self._port.write(CMD_PROGRAM)
                reply = self._port.read(3, timeout=2.0)
                if reply[:2] == b"QX" or (reply and reply[-1] == ACK):
                    self._in_program_mode = True
                    self.drain()
                    self._log("Programmiermodus wieder aufgebaut")
                    return True
            except SerialError:
                pass
            time.sleep(1.0)
        return False

    def _resync(self) -> None:
        time.sleep(0.2)
        try:
            self._port.reset_input()
        except SerialError:
            pass

    # -- Bereiche --------------------------------------------------------
    def read_region(self, addr: int, size: int, progress: Optional[ProgressFn] = None,
                    label: str = "", done: int = 0, total: int = 0) -> bytes:
        total = total or size
        buf = bytearray()
        offset = 0
        while offset < size:
            chunk = min(self.block_size, size - offset)
            buf.extend(self.read_block(addr + offset, chunk))
            offset += chunk
            if progress:
                progress(done + offset, total, label or "Lese 0x%08X" % addr)
        return bytes(buf)

    def write_region(self, addr: int, data: bytes, progress: Optional[ProgressFn] = None,
                     label: str = "", done: int = 0, total: int = 0) -> None:
        """Schreibt einen Bereich - ausgerichtet auf volle Bloecke.

        Das Geraet nimmt nur vollstaendige, ausgerichtete Bloecke an (gemessen:
        16 Byte). Angebrochene Bloecke am Anfang oder Ende quittiert es nicht
        einmal. Deshalb werden die Randbloecke gelesen, mit den neuen Daten
        gemischt und komplett zurueckgeschrieben.
        """
        block = self.write_block_size
        roh_laenge = len(data)
        start = addr - (addr % block)
        ende = addr + len(data)
        ende_ausgerichtet = ((ende + block - 1) // block) * block

        if start != addr:
            kopf = self.read_block(start, block)
            data = kopf[:addr - start] + data
            addr = start
        if ende_ausgerichtet != ende:
            fuss = self.read_block(ende_ausgerichtet - block, block)
            data = data + fuss[block - (ende_ausgerichtet - ende):]

        total = total or roh_laenge
        offset = 0
        while offset < len(data):
            self.write_block(addr + offset, data[offset:offset + block])
            offset += block
            if progress:
                progress(done + min(offset, roh_laenge), total,
                         label or "Schreibe 0x%08X" % addr)

    def verify_region(self, addr: int, data: bytes,
                      progress: Optional[ProgressFn] = None) -> list:
        """Vergleicht den Geraeteinhalt mit ``data``; liefert abweichende Adressen."""
        bad = []
        offset = 0
        while offset < len(data):
            chunk = min(self.block_size, len(data) - offset)
            actual = self.read_block(addr + offset, chunk)
            if actual != data[offset:offset + chunk]:
                bad.append(addr + offset)
            offset += chunk
            if progress:
                progress(offset, len(data), "Pruefe 0x%08X" % addr)
        return bad
