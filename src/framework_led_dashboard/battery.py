from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class BatteryMode(Enum):
    DISCHARGING = "discharging"
    CHARGING = "charging"
    AC = "ac"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BatteryPower:
    mode: BatteryMode
    watts: float | None = None


class BatteryPowerProvider:
    """Read and smooth battery discharge power from Linux power_supply."""

    def __init__(
        self,
        *,
        battery_root: Path = Path("/sys/class/power_supply/BAT1"),
        smoothing_factor: float = 0.35,
    ) -> None:
        if not 0 < smoothing_factor <= 1:
            raise ValueError("smoothing_factor must be greater than zero and at most one")
        self._battery_root = battery_root
        self._smoothing_factor = smoothing_factor
        self._smoothed_watts: float | None = None

    def read(self) -> BatteryPower:
        status = _read_text(self._battery_root / "status")
        mode = _parse_mode(status)
        if mode is not BatteryMode.DISCHARGING:
            self._smoothed_watts = None
            return BatteryPower(mode=mode)

        watts = self._read_discharge_watts()
        if watts is None:
            self._smoothed_watts = None
            return BatteryPower(mode=mode)

        previous = self._smoothed_watts
        if previous is None:
            self._smoothed_watts = watts
        else:
            alpha = self._smoothing_factor
            self._smoothed_watts = alpha * watts + (1 - alpha) * previous
        return BatteryPower(mode=mode, watts=self._smoothed_watts)

    def _read_discharge_watts(self) -> float | None:
        microwatts = _read_nonnegative_number(self._battery_root / "power_now")
        if microwatts is not None:
            return microwatts / 1_000_000

        microamps = _read_nonnegative_number(self._battery_root / "current_now")
        microvolts = _read_nonnegative_number(self._battery_root / "voltage_now")
        if microamps is None or microvolts is None:
            return None
        return microamps * microvolts / 1_000_000_000_000


def _parse_mode(status: str | None) -> BatteryMode:
    if status is None:
        return BatteryMode.UNKNOWN
    normalized = status.casefold()
    if normalized == "discharging":
        return BatteryMode.DISCHARGING
    if normalized == "charging":
        return BatteryMode.CHARGING
    if normalized in {"full", "not charging"}:
        return BatteryMode.AC
    return BatteryMode.UNKNOWN


def _read_nonnegative_number(path: Path) -> float | None:
    raw_value = _read_text(path)
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
