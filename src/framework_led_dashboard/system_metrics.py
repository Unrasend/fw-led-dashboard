from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEVICE_RESCAN_SECONDS = 60.0


class FileSystemStats(Protocol):
    f_blocks: int
    f_bfree: int
    f_bavail: int


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    battery_percent: float | None
    cpu_percent: float | None
    ram_used_percent: float | None
    gpu_percent: float | None = None
    swap_used_percent: float | None = None
    disk_used_percent: float | None = None


class SystemMetricsProvider:
    """Read every value the left panel's bars can show from Linux virtual files."""

    def __init__(
        self,
        *,
        proc_stat_path: Path = Path("/proc/stat"),
        meminfo_path: Path = Path("/proc/meminfo"),
        battery_capacity_path: Path = Path("/sys/class/power_supply/BAT1/capacity"),
        drm_root: Path = Path("/sys/class/drm"),
        disk_path: Path = Path("/"),
        statvfs: Callable[[Path], FileSystemStats] = os.statvfs,
        clock: Callable[[], float] = time.monotonic,
        rescan_seconds: float = DEVICE_RESCAN_SECONDS,
    ) -> None:
        self._proc_stat_path = proc_stat_path
        self._meminfo_path = meminfo_path
        self._battery_capacity_path = battery_capacity_path
        self._drm_root = drm_root
        self._clock = clock
        self._rescan_seconds = rescan_seconds
        self._gpu_files: list[Path] = []
        self._gpu_scanned_at = -math.inf
        self._disk_path = disk_path
        self._statvfs = statvfs
        self._previous_cpu_times: tuple[float, float] | None = None

    def read(self) -> SystemMetrics:
        ram_used, swap_used = self._read_memory_used_percent()
        return SystemMetrics(
            battery_percent=self._read_battery_percent(),
            cpu_percent=self._read_cpu_percent(),
            ram_used_percent=ram_used,
            gpu_percent=self._read_gpu_percent(),
            swap_used_percent=swap_used,
            disk_used_percent=self._read_disk_used_percent(),
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

    def _read_memory_used_percent(self) -> tuple[float | None, float | None]:
        try:
            values = {
                key: float(raw_value.split()[0])
                for line in self._meminfo_path.read_text(encoding="utf-8").splitlines()
                if ":" in line
                for key, raw_value in [line.split(":", 1)]
            }
        except (OSError, ValueError, IndexError):
            return None, None
        ram = _used_percent(values.get("MemTotal"), values.get("MemAvailable"))
        swap = _used_percent(values.get("SwapTotal"), values.get("SwapFree"))
        return ram, swap

    def _read_gpu_percent(self) -> float | None:
        """Busiest awake GPU; a runtime-suspended GPU is skipped so polling never wakes it."""
        readings = []
        for busy_file in self._gpu_busy_files():
            status_file = busy_file.parent / "power" / "runtime_status"
            try:
                if status_file.read_text(encoding="utf-8").strip() == "suspended":
                    continue
            except OSError:
                pass
            try:
                value = float(busy_file.read_text(encoding="utf-8").strip())
            except OSError:
                self._gpu_scanned_at = -math.inf  # gone: look for GPUs again next time
                continue
            except ValueError:
                continue
            if math.isfinite(value) and 0 <= value <= 100:
                readings.append(value)
        return max(readings) if readings else None

    def _gpu_busy_files(self) -> list[Path]:
        """GPU load files, found again every `rescan_seconds` (a glob costs more than a read)."""
        now = self._clock()
        if now - self._gpu_scanned_at >= self._rescan_seconds:
            self._gpu_scanned_at = now
            try:
                self._gpu_files = sorted(self._drm_root.glob("card*/device/gpu_busy_percent"))
            except OSError:
                self._gpu_files = []
        return self._gpu_files

    def _read_disk_used_percent(self) -> float | None:
        try:
            stats = self._statvfs(self._disk_path)
        except OSError:
            return None
        used = stats.f_blocks - stats.f_bfree
        visible = used + stats.f_bavail  # df semantics: ignore blocks reserved for root
        if used < 0 or visible <= 0:
            return None
        return max(0.0, min(100.0, 100 * used / visible))


def _used_percent(total: float | None, free: float | None) -> float | None:
    if total is None or free is None or total <= 0 or free < 0:
        return None
    return max(0.0, min(100.0, 100 * (total - free) / total))
