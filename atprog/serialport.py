"""Serielle Schnittstelle ohne Fremdbibliotheken.

Bevorzugt wird pyserial (falls installiert), sonst greift ein schlanker
POSIX-Backend auf Basis von ``termios``/``select``. Damit laeuft das Programm
auf macOS und Linux ohne jede Installation zusaetzlicher Pakete.
"""
from __future__ import annotations

import glob
import os
import platform
import select
import sys
import time
from typing import List, Optional

try:  # optionaler, bevorzugter Backend
    import serial as _pyserial  # type: ignore
    from serial.tools import list_ports as _pyserial_ports  # type: ignore
except Exception:  # pragma: no cover - pyserial ist optional
    _pyserial = None
    _pyserial_ports = None


class SerialError(IOError):
    """Fehler beim Zugriff auf die serielle Schnittstelle."""


def list_ports() -> List[str]:
    """Liefert moegliche Geraetepfade des Funkgeraets (bester Kandidat zuerst)."""
    if _pyserial_ports is not None:
        names = [p.device for p in _pyserial_ports.comports()]
    elif os.name == "nt":  # pragma: no cover - ohne pyserial nicht ermittelbar
        names = ["COM%d" % i for i in range(1, 33)]
    else:
        names = []
        for pattern in (
            "/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.SLAB*",
            "/dev/cu.wchusbserial*", "/dev/ttyUSB*", "/dev/ttyACM*",
        ):
            names.extend(sorted(glob.glob(pattern)))
    # Bluetooth-/Debug-Ports sind nie das Funkgeraet.
    names = [n for n in names if "Bluetooth" not in n and "debug-console" not in n]

    def score(name: str) -> int:
        low = name.lower()
        if "usbmodem" in low or "ttyacm" in low:
            return 0        # AT-D878UV meldet sich als CDC-ACM
        if "usbserial" in low or "wch" in low or "slab" in low or "ttyusb" in low:
            return 1
        return 2

    seen, out = set(), []
    for name in sorted(names, key=score):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


class SerialPort:
    """Minimaler, blockierender Port mit Timeout-Semantik."""

    def __init__(self, device: str, baudrate: int = 115200, timeout: float = 2.0):
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self._fd: Optional[int] = None
        self._ser = None

    # -- Lebenszyklus ----------------------------------------------------
    def open(self) -> "SerialPort":
        if self.is_open:
            return self
        if _pyserial is not None:
            try:
                self._ser = _pyserial.Serial(
                    self.device, self.baudrate, timeout=self.timeout,
                    write_timeout=self.timeout, rtscts=False, dsrdtr=False,
                )
            except Exception as exc:
                raise SerialError("Port %s kann nicht geoeffnet werden: %s" % (self.device, exc))
            return self
        if os.name == "nt":  # pragma: no cover
            raise SerialError("Unter Windows wird pyserial benoetigt: pip install pyserial")
        self._open_posix()
        return self

    def _open_posix(self) -> None:
        import termios
        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise SerialError("Port %s kann nicht geoeffnet werden: %s" % (self.device, exc))
        try:
            attrs = termios.tcgetattr(fd)
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
            # Rohmodus (entspricht cfmakeraw)
            iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP |
                       termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
            oflag &= ~termios.OPOST
            lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
                       termios.ISIG | termios.IEXTEN)
            cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
            cflag |= termios.CS8 | termios.CLOCAL | termios.CREAD
            if hasattr(termios, "CRTSCTS"):
                cflag &= ~termios.CRTSCTS
            cc = list(cc)
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            speed = getattr(termios, "B%d" % self.baudrate, termios.B115200)
            termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception as exc:
            os.close(fd)
            raise SerialError("Port %s kann nicht konfiguriert werden: %s" % (self.device, exc))
        self._fd = fd

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None or self._fd is not None

    def __enter__(self) -> "SerialPort":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- Datentransfer ---------------------------------------------------
    def reset_input(self) -> None:
        if self._ser is not None:
            self._ser.reset_input_buffer()
            return
        if self._fd is not None:
            import termios
            termios.tcflush(self._fd, termios.TCIFLUSH)

    def write(self, data: bytes) -> int:
        if self._ser is not None:
            n = self._ser.write(data)
            self._ser.flush()
            return int(n or 0)
        if self._fd is None:
            raise SerialError("Port ist nicht geoeffnet")
        total, deadline = 0, time.monotonic() + self.timeout
        while total < len(data):
            if time.monotonic() > deadline:
                raise SerialError("Timeout beim Schreiben auf %s" % self.device)
            _, wfds, _ = select.select([], [self._fd], [], 0.2)
            if not wfds:
                continue
            total += os.write(self._fd, data[total:])
        return total

    def read(self, size: int, timeout: Optional[float] = None) -> bytes:
        """Liest bis zu ``size`` Bytes; gibt bei Timeout weniger zurueck."""
        tmo = self.timeout if timeout is None else timeout
        if self._ser is not None:
            old = self._ser.timeout
            if timeout is not None:
                self._ser.timeout = tmo
            try:
                return self._ser.read(size)
            finally:
                self._ser.timeout = old
        if self._fd is None:
            raise SerialError("Port ist nicht geoeffnet")
        buf, deadline = bytearray(), time.monotonic() + tmo
        while len(buf) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            rfds, _, _ = select.select([self._fd], [], [], min(remaining, 0.2))
            if not rfds:
                continue
            chunk = os.read(self._fd, size - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def read_exact(self, size: int, timeout: Optional[float] = None) -> bytes:
        data = self.read(size, timeout)
        if len(data) != size:
            raise SerialError(
                "Timeout: %d von %d Bytes empfangen (%s)" % (len(data), size, self.device))
        return data


def describe_backend() -> str:
    if _pyserial is not None:
        return "pyserial %s" % getattr(_pyserial, "__version__", "?")
    return "termios (%s)" % platform.system()
