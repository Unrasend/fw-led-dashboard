from __future__ import annotations

import os
import select
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from framework_led_dashboard.device_discovery import discover_matrix_devices
from framework_led_dashboard.serial_port import SerialPort, sysfs_ports


@pytest.fixture
def pty_port() -> Iterator[tuple[int, str]]:
    """A pseudo-terminal: the master end plays the panel, the slave path is the "port"."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    try:
        yield master, path
    finally:
        os.close(slave)
        os.close(master)


def _read_master(master: int, size: int) -> bytes:
    received = b""
    deadline = time.monotonic() + 2
    while len(received) < size and time.monotonic() < deadline:
        if select.select([master], [], [], 0.1)[0]:
            received += os.read(master, size - len(received))
    return received


def test_port_passes_bytes_through_untouched(pty_port: tuple[int, str]) -> None:
    master, path = pty_port
    port = SerialPort(path, read_timeout_seconds=1, write_timeout_seconds=1)
    try:
        # Raw mode: no \n -> \r\n on the way out, no \r -> \n on the way in.
        command = bytes([0x32, 0xAC, 0x07, 0x00]) + b"\n\r\x00\xff"
        assert port.write(command) == len(command)
        assert _read_master(master, len(command)) == command

        os.write(master, b"\r\n\x03\x00" * 8)
        assert port.read(32) == b"\r\n\x03\x00" * 8
    finally:
        port.close()


def test_read_returns_what_arrived_before_the_timeout(pty_port: tuple[int, str]) -> None:
    master, path = pty_port
    port = SerialPort(path, read_timeout_seconds=0.2, write_timeout_seconds=1)
    try:
        os.write(master, b"abc")
        started = time.monotonic()
        assert port.read(32) == b"abc"
        assert 0.15 <= time.monotonic() - started < 1.0
    finally:
        port.close()


def test_write_times_out_when_the_other_end_stops_reading(pty_port: tuple[int, str]) -> None:
    _master, path = pty_port
    port = SerialPort(path, read_timeout_seconds=1, write_timeout_seconds=0.2)
    try:
        with pytest.raises(TimeoutError):
            port.write(b"x" * 1_000_000)  # far more than a tty buffers
    finally:
        port.close()


def test_drops_input_that_was_waiting_before_it_opened(pty_port: tuple[int, str]) -> None:
    master, path = pty_port
    os.write(master, b"stale")
    port = SerialPort(path, read_timeout_seconds=0.1, write_timeout_seconds=1)
    try:
        assert port.read(5) == b""
    finally:
        port.close()


def test_closed_port_raises_os_error(pty_port: tuple[int, str]) -> None:
    _master, path = pty_port
    port = SerialPort(path, read_timeout_seconds=1, write_timeout_seconds=1)
    port.close()
    port.close()  # twice is fine

    with pytest.raises(OSError):
        port.write(b"x")


def test_opening_a_missing_port_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        SerialPort(str(tmp_path / "ttyACM9"), read_timeout_seconds=1, write_timeout_seconds=1)


def _usb_acm_port(
    root: Path, tty: str, *, bus_path: str, serial: str, interfaces: int = 2
) -> None:
    """Lay out /sys the way the kernel does for a USB CDC ACM port."""
    usb_bus = root / "bus" / "usb"
    usb_bus.mkdir(parents=True, exist_ok=True)
    device = root / "devices" / "pci0000:00" / "usb1" / bus_path
    interface = device / f"{bus_path}:1.0"
    interface.mkdir(parents=True)
    (interface / "subsystem").symlink_to(usb_bus)
    for name, value in {
        "idVendor": "32ac",
        "idProduct": "0020",
        "serial": serial,
        "product": "LED Matrix Input Module",
        "bNumInterfaces": f" {interfaces}",
    }.items():
        (device / name).write_text(f"{value}\n", encoding="utf-8")
    (root / "class" / "tty" / tty).mkdir(parents=True)
    (root / "class" / "tty" / tty / "device").symlink_to(interface)


def test_sysfs_ports_name_acm_ports_by_their_usb_interface(tmp_path: Path) -> None:
    _usb_acm_port(tmp_path, "ttyACM0", bus_path="1-3.3", serial="FRAKDEBZ0100000000")
    _usb_acm_port(tmp_path, "ttyACM1", bus_path="1-4.2", serial="FRAKDEBZ0100000000")
    (tmp_path / "class" / "tty" / "ttyS0").mkdir()  # not USB: ignored

    ports = list(sysfs_ports(tmp_path / "class" / "tty", Path("/dev")))

    assert [(port.device, port.location, port.vid, port.pid) for port in ports] == [
        ("/dev/ttyACM0", "1-3.3:1.0", 0x32AC, 0x0020),
        ("/dev/ttyACM1", "1-4.2:1.0", 0x32AC, 0x0020),
    ]
    # Same serial on both, so they are told apart by location, as on real hardware.
    devices = discover_matrix_devices(ports)
    assert [device.identity for device in devices] == ["1-3.3:1.0", "1-4.2:1.0"]


def test_sysfs_ports_name_a_single_interface_device_by_the_device(tmp_path: Path) -> None:
    _usb_acm_port(tmp_path, "ttyACM0", bus_path="3-1", serial="X", interfaces=1)

    (port,) = sysfs_ports(tmp_path / "class" / "tty", Path("/dev"))

    assert port.location == "3-1"
    assert port.serial_number == "X"
