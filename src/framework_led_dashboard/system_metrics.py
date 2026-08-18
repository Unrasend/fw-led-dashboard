from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    battery_percent: float | None
    cpu_percent: float | None
    ram_used_percent: float | None


class SystemMetricsProvider:
    """Read the three values used by the left panel from Linux virtual files."""

    def __init__(
        self,
        *,
        proc_stat_path: Path = Path("/proc/stat"),
        meminfo_path: Path = Path("/proc/meminfo"),
        battery_capacity_path: Path = Path("/sys/class/power_supply/BAT1/capacity"),
    ) -> None:
        self._proc_stat_path = proc_stat_path
        self._meminfo_path = meminfo_path
        self._battery_capacity_path = battery_capacity_path
        self._previous_cpu_times: tuple[float, float] | None = None

    def read(self) -> SystemMetrics:
        return SystemMetrics(
            battery_percent=self._read_battery_percent(),
            cpu_percent=self._read_cpu_percent(),
            ram_used_percent=self._read_ram_used_percent(),
        )

    def _read_battery_percent(self) -> float | None:
        try:
            value = float(self._battery_capacity_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return value if math.isfinite(value) and 0 <= value <= 100 else None

    def _read_cpu_percent(self) -> float | None:
        try:
            first_line = self._proc_stat_path.read_text(encoding="utf-8").splitlines()[0]
            parts = first_line.split()
            if not parts or parts[0] != "cpu":
                return None
            values = tuple(float(value) for value in parts[1:9])
            if len(values) < 4:
                return None
        except (OSError, ValueError, IndexError):
            return None

        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        previous = self._previous_cpu_times
        self._previous_cpu_times = (total, idle)
        if previous is None:
            return None

        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0 or idle_delta < 0:
            return None
        return max(0.0, min(100.0, 100 * (total_delta - idle_delta) / total_delta))

    def _read_ram_used_percent(self) -> float | None:
        try:
            values = {
                key: float(raw_value.split()[0])
                for line in self._meminfo_path.read_text(encoding="utf-8").splitlines()
                if ":" in line
                for key, raw_value in [line.split(":", 1)]
            }
            total = values["MemTotal"]
            available = values["MemAvailable"]
        except (OSError, ValueError, KeyError, IndexError):
            return None

        if total <= 0 or available < 0:
            return None
        return max(0.0, min(100.0, 100 * (total - available) / total))
