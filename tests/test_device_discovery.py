from __future__ import annotations

from dataclasses import dataclass

import pytest

from framework_led_dashboard.config import PanelConfig, PanelsConfig
from framework_led_dashboard.device_discovery import (
    DeviceDiscoveryError,
    MatrixDevice,
    discover_matrix_devices,
    map_panel_devices,
    resolve_device_path,
)


@dataclass
class FakePort:
    device: str
    vid: int | None
    pid: int | None
    serial_number: str | None = None
    location: str | None = None
    product: str | None = None


def test_discovery_accepts_only_framework_led_matrix_usb_ports() -> None:
    ports = [
        FakePort("/dev/ttyACM0", 0x32AC, 0x0020, "matrix-left", "1-1"),
        FakePort("/dev/ttyACM1", 0x32AC, 0x001F, "qtpy", "1-2"),
        FakePort("/dev/ttyACM2", 0x1234, 0x0020, "other-vendor", "1-3"),
        FakePort("/dev/ttyS0", None, None),
    ]

    devices = discover_matrix_devices(ports)

    assert [(device.path, device.identity) for device in devices] == [
        ("/dev/ttyACM0", "matrix-left")
    ]


def test_discovery_uses_location_when_framework_panels_share_one_serial_number() -> None:
    devices = discover_matrix_devices(
        [
            FakePort("/dev/ttyACM0", 0x32AC, 0x0020, "shared-serial", "1-3.3:1.0"),
            FakePort("/dev/ttyACM1", 0x32AC, 0x0020, "shared-serial", "1-4.2:1.0"),
        ]
    )

    assert [(device.path, device.identity) for device in devices] == [
        ("/dev/ttyACM0", "1-3.3:1.0"),
        ("/dev/ttyACM1", "1-4.2:1.0"),
    ]


@pytest.mark.parametrize(
    ("swap_panels", "expected_left", "expected_right"),
    [(False, "/dev/ttyACM1", "/dev/ttyACM0"), (True, "/dev/ttyACM0", "/dev/ttyACM1")],
)
def test_mapping_uses_configured_identity_before_optional_swap(
    swap_panels: bool,
    expected_left: str,
    expected_right: str,
) -> None:
    devices = discover_matrix_devices(
        [
            FakePort("/dev/ttyACM0", 0x32AC, 0x0020, None, "usb-right"),
            FakePort("/dev/ttyACM1", 0x32AC, 0x0020, "serial-left", "usb-left"),
        ]
    )
    configured_panels = PanelsConfig(
        left=PanelConfig(identity="serial-left"),
        right=PanelConfig(identity="usb-right"),
    )

    mapped = map_panel_devices(devices, configured_panels, swap_panels=swap_panels)

    assert mapped.left.path == expected_left
    assert mapped.right.path == expected_right


def test_mapping_refuses_to_guess_when_identity_is_not_configured() -> None:
    devices = discover_matrix_devices(
        [
            FakePort("/dev/ttyACM0", 0x32AC, 0x0020, "one", "1-1"),
            FakePort("/dev/ttyACM1", 0x32AC, 0x0020, "two", "1-2"),
        ]
    )

    with pytest.raises(DeviceDiscoveryError, match="configure identities"):
        map_panel_devices(devices, PanelsConfig(), swap_panels=False)


def test_mapping_rejects_two_aliases_that_resolve_to_one_physical_device() -> None:
    devices = discover_matrix_devices(
        [FakePort("/dev/ttyACM0", 0x32AC, 0x0020, "serial-left", "usb-location-left")]
    )
    configured_panels = PanelsConfig(
        left=PanelConfig(identity="serial-left"),
        right=PanelConfig(identity="usb-location-left"),
    )

    with pytest.raises(DeviceDiscoveryError, match="same physical device"):
        map_panel_devices(devices, configured_panels, swap_panels=False)


def test_resolver_tracks_identity_when_tty_numbers_change() -> None:
    current_ports = [
        FakePort("/dev/ttyACM0", 0x32AC, 0x0020, "stable-left", "1-1"),
        FakePort("/dev/ttyACM1", 0x32AC, 0x0020, "stable-right", "1-2"),
    ]

    assert resolve_device_path("stable-left", lambda: current_ports) == "/dev/ttyACM0"

    current_ports[:] = [
        FakePort("/dev/ttyACM0", 0x32AC, 0x0020, "stable-right", "1-2"),
        FakePort("/dev/ttyACM1", 0x32AC, 0x0020, "stable-left", "1-1"),
    ]

    assert resolve_device_path("stable-left", lambda: current_ports) == "/dev/ttyACM1"


def test_resolver_rejects_missing_and_ambiguous_identity() -> None:
    missing_ports = [FakePort("/dev/ttyACM0", 0x32AC, 0x0020, "other", "1-1")]
    with pytest.raises(DeviceDiscoveryError, match="not connected"):
        resolve_device_path("wanted", lambda: missing_ports)

    duplicate_ports = [
        FakePort("/dev/ttyACM0", 0x32AC, 0x0020, "duplicate", "1-1"),
        FakePort("/dev/ttyACM1", 0x32AC, 0x0020, "duplicate", "1-2"),
    ]
    with pytest.raises(DeviceDiscoveryError, match="ambiguous"):
        resolve_device_path("duplicate", lambda: duplicate_ports)


def test_list_panels_cli_prints_calibration_identity_and_location(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from framework_led_dashboard.__main__ import main

    devices = (
        MatrixDevice(
            path="/dev/ttyACM0",
            serial_number="serial-left",
            location="1-1",
            product="LED Matrix Input Module",
            identity="serial-left",
        ),
        MatrixDevice(
            path="/dev/ttyACM1",
            serial_number=None,
            location="1-2",
            product="LED Matrix Input Module",
            identity="1-2",
        ),
    )

    exit_code = main(["--list-panels"], panel_lister=lambda: devices)

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {
        "panels": [
            {
                "identity": "serial-left",
                "location": "1-1",
                "path": "/dev/ttyACM0",
                "product": "LED Matrix Input Module",
                "serial_number": "serial-left",
            },
            {
                "identity": "1-2",
                "location": "1-2",
                "path": "/dev/ttyACM1",
                "product": "LED Matrix Input Module",
                "serial_number": None,
            },
        ]
    }
