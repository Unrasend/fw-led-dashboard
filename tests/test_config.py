from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework_led_dashboard.config import ConfigError, load_config, parse_config


def test_defaults_match_dashboard_decisions() -> None:
    config = parse_config({})

    assert config.brightness_percent == 40
    assert config.animation_fps == 10
    assert config.swap_panels is False
    assert config.updates.metrics_seconds == 1
    assert config.updates.weather_seconds == 15 * 60
    assert config.location.ip_lookup_ttl_seconds == 6 * 60 * 60
    assert config.alerts.cpu_temperature_critical_c == 120
    assert config.alerts.cpu_temperature_recovery_c == 115
    assert config.alerts.battery_critical_percent == 10
    assert config.alerts.battery_recovery_percent == 12
    assert config.alerts.ram_available_critical_percent == 5
    assert config.alerts.ram_available_recovery_percent == 8
    assert config.alerts.ram_critical_seconds == 10
    assert config.alerts.cpu_usage_critical_percent is None
    assert config.alerts.outside_temperature_min_c is None
    assert config.alerts.outside_temperature_max_c is None


def test_load_config_normalizes_optional_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
brightness_percent = 55
animation_fps = 4
swap_panels = true

[panels.left]
identity = "  left-serial  "

[panels.right]
identity = "right-serial"

[location]
latitude = 50.0755
longitude = 14.4378
vpn_interface_prefixes = ["tun", "wg", " tailscale "]

[alerts]
cpu_usage_critical_percent = 95
outside_temperature_min_c = -20
outside_temperature_max_c = 38
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.brightness_percent == 55
    assert config.animation_fps == 4
    assert config.swap_panels is True
    assert config.panels.left.identity == "left-serial"
    assert config.panels.right.identity == "right-serial"
    assert config.location.latitude == 50.0755
    assert config.location.longitude == 14.4378
    assert config.location.vpn_interface_prefixes == ("tun", "wg", "tailscale")
    assert config.alerts.cpu_usage_critical_percent == 95
    assert config.alerts.outside_temperature_min_c == -20
    assert config.alerts.outside_temperature_max_c == 38


@pytest.mark.parametrize("brightness", [-1, 101, True, 40.5, "40"])
def test_rejects_invalid_brightness(brightness: object) -> None:
    with pytest.raises(ConfigError, match="brightness_percent"):
        parse_config({"brightness_percent": brightness})


@pytest.mark.parametrize("fps", [0, 31, True, 2.5, "2"])
def test_rejects_invalid_animation_fps(fps: object) -> None:
    with pytest.raises(ConfigError, match="animation_fps"):
        parse_config({"animation_fps": fps})


@pytest.mark.parametrize(
    "location",
    [
        {"latitude": 50.0},
        {"longitude": 14.0},
        {"latitude": -90.1, "longitude": 14.0},
        {"latitude": 90.1, "longitude": 14.0},
        {"latitude": 50.0, "longitude": -180.1},
        {"latitude": 50.0, "longitude": 180.1},
        {"latitude": True, "longitude": 14.0},
    ],
)
def test_rejects_invalid_coordinates(location: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="location"):
        parse_config({"location": location})


def test_rejects_duplicate_panel_identity_after_normalization() -> None:
    with pytest.raises(ConfigError, match="different identities"):
        parse_config(
            {
                "panels": {
                    "left": {"identity": " matrix-1 "},
                    "right": {"identity": "matrix-1"},
                }
            }
        )


@pytest.mark.parametrize(
    ("alerts", "message"),
    [
        (
            {"cpu_temperature_critical_c": 120, "cpu_temperature_recovery_c": 120},
            "cpu_temperature_recovery_c",
        ),
        (
            {"battery_critical_percent": 10, "battery_recovery_percent": 10},
            "battery_recovery_percent",
        ),
        (
            {"ram_available_critical_percent": 5, "ram_available_recovery_percent": 5},
            "ram_available_recovery_percent",
        ),
        ({"ram_critical_seconds": 0}, "ram_critical_seconds"),
        (
            {"cpu_temperature_critical_c": 39, "cpu_temperature_recovery_c": 30},
            "between 40 and 150",
        ),
        ({"cpu_temperature_critical_c": 151}, "cpu_temperature_critical_c"),
        ({"cpu_usage_critical_percent": 101}, "cpu_usage_critical_percent"),
        (
            {"outside_temperature_min_c": 30, "outside_temperature_max_c": 20},
            "outside_temperature_min_c",
        ),
    ],
)
def test_rejects_invalid_alert_thresholds(alerts: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config({"alerts": alerts})


def test_rejects_unknown_keys_to_catch_configuration_typos() -> None:
    with pytest.raises(ConfigError, match="unknown config key: brightnes_percent"):
        parse_config({"brightnes_percent": 40})


def test_cli_check_config_prints_normalized_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from framework_led_dashboard.__main__ import main

    config_path = tmp_path / "config.toml"
    config_path.write_text("brightness_percent = 40\n", encoding="utf-8")

    exit_code = main(["--check-config", str(config_path)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["brightness_percent"] == 40
    assert output["animation_fps"] == 10
    assert output["panels"]["left"]["identity"] is None


EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.toml"


def _device(identity: str):
    from framework_led_dashboard.device_discovery import MatrixDevice

    return MatrixDevice(
        path=f"/dev/{identity}",
        serial_number="FRAKDEBZ0100000000",
        location=identity,
        product="LED Matrix Input Module",
        identity=identity,
    )


def test_fill_panel_identities_changes_only_the_two_identity_lines() -> None:
    from framework_led_dashboard.config import fill_panel_identities

    template = EXAMPLE_CONFIG.read_text(encoding="utf-8")

    filled = fill_panel_identities(template, left="1-4.2:1.0", right="1-3.3:1.0")

    config = parse_config(__import__("tomllib").loads(filled))
    assert config.panels.left.identity == "1-4.2:1.0"
    assert config.panels.right.identity == "1-3.3:1.0"
    changed = [
        (old, new)
        for old, new in zip(template.splitlines(), filled.splitlines(), strict=True)
        if old != new
    ]
    assert changed == [
        ('identity = ""', 'identity = "1-4.2:1.0"'),
        ('identity = ""', 'identity = "1-3.3:1.0"'),
    ]


def test_fill_panel_identities_needs_both_panel_sections() -> None:
    from framework_led_dashboard.config import fill_panel_identities

    with pytest.raises(ConfigError, match="panels.right"):
        fill_panel_identities('[panels.left]\nidentity = ""\n', left="a", right="b")


def test_cli_init_config_writes_detected_panels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from framework_led_dashboard.__main__ import main

    target = tmp_path / "config" / "config.toml"
    devices = [_device("1-3.3:1.0"), _device("1-4.2:1.0")]

    exit_code = main(
        ["--init-config", str(EXAMPLE_CONFIG), str(target)], panel_lister=lambda: devices
    )

    assert exit_code == 0
    config = load_config(target)
    # On the Framework 16 this was calibrated on, the higher hub port is the left module.
    assert config.panels.left.identity == "1-4.2:1.0"
    assert config.panels.right.identity == "1-3.3:1.0"
    assert "Swap panels" in capsys.readouterr().out


def test_cli_init_config_never_overwrites_an_existing_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from framework_led_dashboard.__main__ import main

    target = tmp_path / "config.toml"
    target.write_text("brightness_percent = 70\n", encoding="utf-8")

    exit_code = main(
        ["--init-config", str(EXAMPLE_CONFIG), str(target)],
        panel_lister=lambda: [_device("a"), _device("b")],
    )

    assert exit_code == 2
    assert target.read_text(encoding="utf-8") == "brightness_percent = 70\n"
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize("identities", [[], ["1-3.3:1.0"], ["a", "b", "c"]])
def test_cli_init_config_needs_exactly_two_panels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    identities: list[str],
) -> None:
    from framework_led_dashboard.__main__ import main

    target = tmp_path / "config.toml"

    exit_code = main(
        ["--init-config", str(EXAMPLE_CONFIG), str(target)],
        panel_lister=lambda: [_device(identity) for identity in identities],
    )

    assert exit_code == 2
    assert not target.exists()
    assert f"found {len(identities)}" in capsys.readouterr().err
