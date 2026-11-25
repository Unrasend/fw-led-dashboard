"""The LED Matrix's USB serial port, with nothing but the standard library.

It replaces pyserial and behaves like pyserial's Linux Serial does for what the dashboard
uses, so the panels see exactly the same traffic:

- raw 8N1 mode, no flow control, DTR and RTS raised, pending input dropped on open;
- write() waits after each write until the port takes more data: USB flow control is how
  the firmware paces the host, and a write that cannot finish in time raises TimeoutError;
- read(size) collects up to `size` bytes and returns what arrived before the timeout.

It also lists LED-Matrix-capable ports from sysfs, the way pyserial's list_ports does.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import struct
import termios
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


class SerialPort:
    def __init__(
        self,
        path: str,
        *,
        read_timeout_seconds: float,
        write_timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_timeout = read_timeout_seconds
        self._write_timeout = write_timeout_seconds
        self._clock = clock
        self._fd: int | None = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            _configure_raw(self._fd)
            _raise_modem_lines(self._fd)
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except BaseException:
            self.close()
            raise

    def write(self, data: bytes) -> int:
        fd = self._require_open()
        view = memoryview(data)
        deadline = self._clock() + self._write_timeout
        written = 0
        while written < len(view):
            try:
                written += os.write(fd, view[written:])
            except BlockingIOError:
                pass
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("serial write timed out")
            _readable, writable, _error = select.select([], [fd], [], remaining)
            if not writable:
                raise TimeoutError("serial write timed out")
        return written

    def read(self, size: int) -> bytes:
        fd = self._require_open()
        received = bytearray()
        deadline = self._clock() + self._read_timeout
        while len(received) < size:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            readable, _writable, _error = select.select([fd], [], [], remaining)
            if not readable:
                break
            try:
                chunk = os.read(fd, size - len(received))
            except BlockingIOError:
                continue
            if not chunk:
                raise OSError(errno.EIO, "serial port is readable but returned no data")
            received += chunk
        return bytes(received)

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def _require_open(self) -> int:
        if self._fd is None:
            raise OSError(errno.EBADF, "serial port is closed")
        return self._fd


def _configure_raw(fd: int) -> None:
    iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(fd)
    cflag |= termios.CLOCAL | termios.CREAD
    cflag &= ~(termios.CSIZE | termios.CSTOPB | termios.PARENB | termios.PARODD)
    cflag |= termios.CS8
    cflag &= ~getattr(termios, "CRTSCTS", 0)
    lflag &= ~(
        termios.ICANON
        | termios.ECHO
        | termios.ECHOE
        | termios.ECHOK
        | termios.ECHONL
        | termios.ISIG
        | termios.IEXTEN
        | getattr(termios, "ECHOCTL", 0)
        | getattr(termios, "ECHOKE", 0)
    )
    oflag &= ~(termios.OPOST | termios.ONLCR | termios.OCRNL)
    iflag &= ~(
        termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IGNBRK
        | termios.INPCK
        | termios.ISTRIP
        | termios.IXON
        | termios.IXOFF
        | termios.IXANY
        | getattr(termios, "IUCLC", 0)
        | getattr(termios, "PARMRK", 0)
    )
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    # USB CDC ignores the baud rate; set the one the firmware documents anyway.
    speed = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])


def _raise_modem_lines(fd: int) -> None:
    lines = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)
    try:
        fcntl.ioctl(fd, termios.TIOCMBIS, lines)
    except OSError as error:
        # Pseudo-terminals (used in tests) have no modem lines.
        if error.errno not in (errno.EINVAL, errno.ENOTTY):
            raise


@dataclass(frozen=True, slots=True)
class SysfsPort:
    device: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    location: str | None
    product: str | None


def sysfs_ports(
    tty_root: Path = Path("/sys/class/tty"), dev_root: Path = Path("/dev")
) -> Iterator[SysfsPort]:
    """USB CDC ACM ports with their USB attributes, named like pyserial's list_ports."""
    for tty in sorted(tty_root.glob("ttyACM*")):
        try:
            interface = (tty / "device").resolve(strict=True)
            if (interface / "subsystem").resolve(strict=True).name != "usb":
                continue
        except OSError:
            continue
        usb_device = interface.parent
        interfaces = _read_int(usb_device / "bNumInterfaces", base=10)
        # A composite device (a CDC ACM port always is) is named by its interface, 1-4.2:1.0.
        location = interface.name if interfaces is None or interfaces > 1 else usb_device.name
        yield SysfsPort(
            device=str(dev_root / tty.name),
            vid=_read_int(usb_device / "idVendor", base=16),
            pid=_read_int(usb_device / "idProduct", base=16),
            serial_number=_read_text(usb_device / "serial"),
            location=location,
            product=_read_text(usb_device / "product"),
        )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path, *, base: int) -> int | None:
    text = _read_text(path)
    try:
        return None if text is None else int(text, base)
    except ValueError:
        return None
