from __future__ import annotations

import math
from pathlib import Path

_CPU_LABELS = {"cpu", "cpu temp", "cpu@4c", "package id 0", "tctl"}
_CPU_DRIVERS = {"coretemp", "k10temp"}


class CpuTemperatureProvider:
    """Read the best available CPU temperature from Linux hwmon."""

    def __init__(self, *, hwmon_root: Path = Path("/sys/class/hwmon")) -> None:
        self._hwmon_root = hwmon_root

    def read_celsius(self) -> float | None:
        candidates: list[tuple[int, str, float]] = []
        try:
            hwmon_directories = sorted(self._hwmon_root.glob("hwmon*"))
        except OSError:
            return None

        for hwmon_directory in hwmon_directories:
            driver = _read_text(hwmon_directory / "name")
            if driver is None:
                continue
            driver = driver.casefold()

            for input_path in sorted(hwmon_directory.glob("temp*_input")):
                label_path = input_path.with_name(input_path.name.replace("_input", "_label"))
                label = (_read_text(label_path) or "").casefold()
                priority = _sensor_priority(driver, label)
                if priority is None:
                    continue

                temperature = _read_millidegrees(input_path)
                if temperature is not None:
                    candidates.append((priority, str(input_path), temperature))

        if not candidates:
            return None
        return min(candidates)[2]


def _sensor_priority(driver: str, label: str) -> int | None:
    if driver == "k10temp" and label == "tctl":
        return 0
    if label in _CPU_LABELS:
        return 1
    if driver in _CPU_DRIVERS:
        return 2
    return None


def _read_millidegrees(path: Path) -> float | None:
    raw_value = _read_text(path)
    if raw_value is None:
        return None
    try:
        temperature = float(raw_value) / 1000
    except ValueError:
        return None
    if not math.isfinite(temperature) or not -273.15 <= temperature <= 250:
        return None
    return temperature


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
