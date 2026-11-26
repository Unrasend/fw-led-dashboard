from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .config import PanelsConfig
from .serial_port import sysfs_ports

FRAMEWORK_VID = 0x32AC
LED_MATRIX_PID = 0x0020


class DeviceDiscoveryError(RuntimeError):
    """Raised when configured panels cannot be mapped safely."""


class PortInfo(Protocol):
    device: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    location: str | None
    product: str | None


PortProvider = Callable[[], Iterable[PortInfo]]


@dataclass(frozen=True, slots=True)
class MatrixDevice:
    path: str
    serial_number: str | None
    location: str | None
    product: str | None
    identity: str | None

    def matches(self, identity: str) -> bool:
        return identity in {self.serial_number, self.location}


@dataclass(frozen=True, slots=True)
class PanelDevices:
    left: MatrixDevice
    right: MatrixDevice


def discover_matrix_devices(ports: Iterable[PortInfo]) -> tuple[MatrixDevice, ...]:
    detected = [
        MatrixDevice(
            path=port.device,
            serial_number=_normalize(port.serial_number),
            location=_normalize(port.location),
            product=_normalize(port.product),
            identity=None,
        )
        for port in ports
        if port.vid == FRAMEWORK_VID and port.pid == LED_MATRIX_PID
    ]
    devices = [
        replace(device, identity=_unique_identity(device, detected)) for device in detected
    ]
    return tuple(sorted(devices, key=lambda device: (device.identity or "", device.path)))


def list_system_matrix_devices(
    port_provider: PortProvider | None = None,
) -> tuple[MatrixDevice, ...]:
    provider = port_provider or _system_ports
    return discover_matrix_devices(provider())


def map_panel_devices(
    devices: Iterable[MatrixDevice],
    configured_panels: PanelsConfig,
    *,
    swap_panels: bool,
) -> PanelDevices:
    left_identity = configured_panels.left.identity
    right_identity = configured_panels.right.identity
    if left_identity is None or right_identity is None:
        raise DeviceDiscoveryError("configure identities for both panels before starting")

    available = tuple(devices)
    left = _find_unique(available, left_identity)
    right = _find_unique(available, right_identity)
    if left.path == right.path:
        raise DeviceDiscoveryError("left and right identities resolve to the same physical device")

    mapped = PanelDevices(left=left, right=right)
    if swap_panels:
        return PanelDevices(left=mapped.right, right=mapped.left)
    return mapped


def resolve_device_path(identity: str, port_provider: PortProvider | None = None) -> str:
    devices = list_system_matrix_devices(port_provider)
    return _find_unique(devices, identity).path


def unreachable_matrix_ports(
    tty_root: Path = Path("/sys/class/tty"), dev_root: Path = Path("/dev")
) -> tuple[str, ...]:
    """LED Matrix ports the kernel has but /dev cannot open: no node, or a node of another device.

    Only the Docker variant meets them: its /dev holds the ports that were there when the
    container started, so a module that comes back as another ttyACM (after a resume) is not.
    """
    unreachable = []
    for port in sysfs_ports(tty_root, dev_root):
        if port.vid != FRAMEWORK_VID or port.pid != LED_MATRIX_PID:
            continue
        name = Path(port.device).name
        try:
            major, minor = map(int, (tty_root / name / "dev").read_text().split(":"))
        except (OSError, ValueError):
            continue  # gone again since the listing
        try:
            node = os.stat(port.device)
        except OSError:
            unreachable.append(name)
            continue
        if not stat.S_ISCHR(node.st_mode) or node.st_rdev != os.makedev(major, minor):
            unreachable.append(name)
    return tuple(unreachable)


def _find_unique(devices: Iterable[MatrixDevice], identity: str) -> MatrixDevice:
    matches = [device for device in devices if device.matches(identity)]
    if not matches:
        raise DeviceDiscoveryError(f"matrix identity {identity!r} is not connected")
    if len(matches) > 1:
        raise DeviceDiscoveryError(f"matrix identity {identity!r} is ambiguous")
    return matches[0]


def _system_ports() -> Iterable[PortInfo]:
    return sysfs_ports()


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _unique_identity(device: MatrixDevice, devices: Iterable[MatrixDevice]) -> str | None:
    for candidate in (device.serial_number, device.location):
        if candidate is not None and sum(other.matches(candidate) for other in devices) == 1:
            return candidate
    return None
