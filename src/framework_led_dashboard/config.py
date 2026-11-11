from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when dashboard configuration is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class PanelConfig:
    identity: str | None = None


@dataclass(frozen=True, slots=True)
class PanelsConfig:
    left: PanelConfig = PanelConfig()
    right: PanelConfig = PanelConfig()


@dataclass(frozen=True, slots=True)
class LocationConfig:
    latitude: float | None = None
    longitude: float | None = None
    ip_lookup_enabled: bool = True
    ip_lookup_ttl_seconds: int = 6 * 60 * 60
    vpn_interface_prefixes: tuple[str, ...] = (
        "tun",
        "tap",
        "wg",
        "tailscale",
        "nordlynx",
        "mullvad",
    )


@dataclass(frozen=True, slots=True)
class UpdateIntervalsConfig:
    metrics_seconds: int = 1
    weather_seconds: int = 15 * 60


@dataclass(frozen=True, slots=True)
class AlertsConfig:
    cpu_temperature_critical_c: float = 120
    cpu_temperature_recovery_c: float = 115
    battery_critical_percent: float = 10
    battery_recovery_percent: float = 12
    ram_available_critical_percent: float = 5
    ram_available_recovery_percent: float = 8
    ram_critical_seconds: float = 10
    cpu_usage_critical_percent: float | None = None
    outside_temperature_min_c: float | None = None
    outside_temperature_max_c: float | None = None


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    brightness_percent: int = 40
    # Frame-rate cap per panel; grayscale tops out near 7 FPS in firmware, so 10 = hardware-bound.
    animation_fps: int = 10
    swap_panels: bool = False
    panels: PanelsConfig = PanelsConfig()
    location: LocationConfig = LocationConfig()
    updates: UpdateIntervalsConfig = UpdateIntervalsConfig()
    alerts: AlertsConfig = AlertsConfig()


def load_config(path: str | Path) -> DashboardConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {config_path}: {error}") from error

    return parse_config(raw)


def fill_panel_identities(template: str, *, left: str, right: str) -> str:
    """Return `template` (TOML text) with the identity lines of both panel sections replaced.

    Works on text so the template's comments and layout survive; the result is validated.
    """
    lines = template.splitlines(keepends=True)
    wanted = {"panels.left": left, "panels.right": right}
    section = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").strip()
        elif section in wanted and stripped.split("=", 1)[0].strip() == "identity":
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"identity = {_toml_string(wanted.pop(section))}{ending}"
    if wanted:
        missing = ", ".join(f"[{name}] identity" for name in wanted)
        raise ConfigError(f"template has no {missing} line")
    filled = "".join(lines)
    try:
        parse_config(tomllib.loads(filled))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"filled template is not valid TOML: {error}") from error
    return filled


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_config(raw: Mapping[str, Any]) -> DashboardConfig:
    _reject_unknown(
        raw,
        {
            "brightness_percent",
            "animation_fps",
            "swap_panels",
            "panels",
            "location",
            "updates",
            "alerts",
        },
    )

    brightness = _integer(raw, "brightness_percent", 40)
    if not 0 <= brightness <= 100:
        raise ConfigError("brightness_percent must be between 0 and 100")

    animation_fps = _integer(raw, "animation_fps", 10)
    if not 1 <= animation_fps <= 30:
        raise ConfigError("animation_fps must be between 1 and 30")

    swap_panels = _boolean(raw, "swap_panels", False)
    panels = _parse_panels(_table(raw, "panels"))
    location = _parse_location(_table(raw, "location"))
    updates = _parse_updates(_table(raw, "updates"))
    alerts = _parse_alerts(_table(raw, "alerts"))

    return DashboardConfig(
        brightness_percent=brightness,
        animation_fps=animation_fps,
        swap_panels=swap_panels,
        panels=panels,
        location=location,
        updates=updates,
        alerts=alerts,
    )


def _parse_panels(raw: Mapping[str, Any]) -> PanelsConfig:
    _reject_unknown(raw, {"left", "right"}, "panels")
    left = _parse_panel(_table(raw, "left"), "panels.left")
    right = _parse_panel(_table(raw, "right"), "panels.right")

    if left.identity is not None and left.identity == right.identity:
        raise ConfigError("left and right panels must use different identities")

    return PanelsConfig(left=left, right=right)


def _parse_panel(raw: Mapping[str, Any], path: str) -> PanelConfig:
    _reject_unknown(raw, {"identity"}, path)
    identity = raw.get("identity")
    if identity is None:
        return PanelConfig()
    if not isinstance(identity, str):
        raise ConfigError(f"{path}.identity must be a string")

    normalized_identity = identity.strip()
    return PanelConfig(identity=normalized_identity or None)


def _parse_location(raw: Mapping[str, Any]) -> LocationConfig:
    _reject_unknown(
        raw,
        {
            "latitude",
            "longitude",
            "ip_lookup_enabled",
            "ip_lookup_ttl_seconds",
            "vpn_interface_prefixes",
        },
        "location",
    )
    latitude = _optional_number(raw, "latitude", "location")
    longitude = _optional_number(raw, "longitude", "location")

    if (latitude is None) != (longitude is None):
        raise ConfigError("location.latitude and location.longitude must be configured together")
    if latitude is not None and not -90 <= latitude <= 90:
        raise ConfigError("location.latitude must be between -90 and 90")
    if longitude is not None and not -180 <= longitude <= 180:
        raise ConfigError("location.longitude must be between -180 and 180")

    ip_lookup_enabled = _boolean(raw, "ip_lookup_enabled", True, "location")
    ip_lookup_ttl_seconds = _integer(raw, "ip_lookup_ttl_seconds", 6 * 60 * 60, "location")
    if ip_lookup_ttl_seconds <= 0:
        raise ConfigError("location.ip_lookup_ttl_seconds must be greater than zero")

    default_prefixes = LocationConfig().vpn_interface_prefixes
    prefixes = _string_tuple(raw, "vpn_interface_prefixes", default_prefixes, "location")
    if not prefixes:
        raise ConfigError("location.vpn_interface_prefixes must not be empty")

    return LocationConfig(
        latitude=latitude,
        longitude=longitude,
        ip_lookup_enabled=ip_lookup_enabled,
        ip_lookup_ttl_seconds=ip_lookup_ttl_seconds,
        vpn_interface_prefixes=prefixes,
    )


def _parse_updates(raw: Mapping[str, Any]) -> UpdateIntervalsConfig:
    _reject_unknown(raw, {"metrics_seconds", "weather_seconds"}, "updates")
    metrics_seconds = _integer(raw, "metrics_seconds", 1, "updates")
    weather_seconds = _integer(raw, "weather_seconds", 15 * 60, "updates")
    if metrics_seconds <= 0:
        raise ConfigError("updates.metrics_seconds must be greater than zero")
    if weather_seconds <= 0:
        raise ConfigError("updates.weather_seconds must be greater than zero")

    return UpdateIntervalsConfig(metrics_seconds=metrics_seconds, weather_seconds=weather_seconds)


def _parse_alerts(raw: Mapping[str, Any]) -> AlertsConfig:
    allowed_keys = {
        "cpu_temperature_critical_c",
        "cpu_temperature_recovery_c",
        "battery_critical_percent",
        "battery_recovery_percent",
        "ram_available_critical_percent",
        "ram_available_recovery_percent",
        "ram_critical_seconds",
        "cpu_usage_critical_percent",
        "outside_temperature_min_c",
        "outside_temperature_max_c",
    }
    _reject_unknown(raw, allowed_keys, "alerts")

    cpu_critical = _number(raw, "cpu_temperature_critical_c", 120, "alerts")
    cpu_recovery = _number(raw, "cpu_temperature_recovery_c", 115, "alerts")
    if cpu_recovery >= cpu_critical:
        raise ConfigError("alerts.cpu_temperature_recovery_c must be below the critical value")

    battery_critical = _percentage(raw, "battery_critical_percent", 10, "alerts")
    battery_recovery = _percentage(raw, "battery_recovery_percent", 12, "alerts")
    if battery_recovery <= battery_critical:
        raise ConfigError("alerts.battery_recovery_percent must be above the critical value")

    ram_critical = _percentage(raw, "ram_available_critical_percent", 5, "alerts")
    ram_recovery = _percentage(raw, "ram_available_recovery_percent", 8, "alerts")
    if ram_recovery <= ram_critical:
        raise ConfigError("alerts.ram_available_recovery_percent must be above the critical value")

    ram_critical_seconds = _number(raw, "ram_critical_seconds", 10, "alerts")
    if ram_critical_seconds <= 0:
        raise ConfigError("alerts.ram_critical_seconds must be greater than zero")

    cpu_usage_critical = _optional_number(raw, "cpu_usage_critical_percent", "alerts")
    if cpu_usage_critical is not None and not 0 < cpu_usage_critical <= 100:
        raise ConfigError("alerts.cpu_usage_critical_percent must be above 0 and at most 100")

    outside_min = _optional_number(raw, "outside_temperature_min_c", "alerts")
    outside_max = _optional_number(raw, "outside_temperature_max_c", "alerts")
    if outside_min is not None and outside_max is not None and outside_min >= outside_max:
        raise ConfigError(
            "alerts.outside_temperature_min_c must be below outside_temperature_max_c"
        )

    return AlertsConfig(
        cpu_temperature_critical_c=cpu_critical,
        cpu_temperature_recovery_c=cpu_recovery,
        battery_critical_percent=battery_critical,
        battery_recovery_percent=battery_recovery,
        ram_available_critical_percent=ram_critical,
        ram_available_recovery_percent=ram_recovery,
        ram_critical_seconds=ram_critical_seconds,
        cpu_usage_critical_percent=cpu_usage_critical,
        outside_temperature_min_c=outside_min,
        outside_temperature_max_c=outside_max,
    )


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], path: str = "") -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        key = f"{path}.{unknown[0]}" if path else unknown[0]
        raise ConfigError(f"unknown config key: {key}")


def _table(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be a TOML table")
    return value


def _integer(raw: Mapping[str, Any], key: str, default: int, path: str = "") -> int:
    value = raw.get(key, default)
    name = f"{path}.{key}" if path else key
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    return value


def _number(raw: Mapping[str, Any], key: str, default: float, path: str) -> float:
    value = raw.get(key, default)
    name = f"{path}.{key}"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number")
    return float(value)


def _optional_number(raw: Mapping[str, Any], key: str, path: str) -> float | None:
    if key not in raw or raw[key] is None:
        return None
    return _number(raw, key, 0, path)


def _percentage(raw: Mapping[str, Any], key: str, default: float, path: str) -> float:
    value = _number(raw, key, default, path)
    if not 0 <= value <= 100:
        raise ConfigError(f"{path}.{key} must be between 0 and 100")
    return value


def _boolean(raw: Mapping[str, Any], key: str, default: bool, path: str = "") -> bool:
    value = raw.get(key, default)
    name = f"{path}.{key}" if path else key
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _string_tuple(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[str, ...],
    path: str,
) -> tuple[str, ...]:
    value = raw.get(key, default)
    name = f"{path}.{key}"
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ConfigError(f"{name} must be an array of strings")

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} must contain non-empty strings")
        prefix = item.strip()
        if prefix not in normalized:
            normalized.append(prefix)
    return tuple(normalized)
