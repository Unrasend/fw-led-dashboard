from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from pathlib import Path

SENSOR_RESCAN_SECONDS = 60.0

_CPU_LABELS = {"cpu", "cpu temp", "cpu@4c", "package id 0", "tctl"}
_CPU_DRIVERS = {"coretemp", "k10temp"}


class CpuTemperatureProvider:
    """Read the best available CPU temperature from Linux hwmon.

    Finding the sensor means walking every hwmon directory, so the choice is kept and only
    its file is read; the search runs again every `rescan_seconds` or when that read fails.
    """

    def __init__(
        self,
        *,
        hwmon_root: Path = Path("/sys/class/hwmon"),
        clock: Callable[[], float] = time.monotonic,
        rescan_seconds: float = SENSOR_RESCAN_SECONDS,
    ) -> None:
        self._hwmon_root = hwmon_root
        self._clock = clock
        self._rescan_seconds = rescan_seconds
        self._sensor: Path | None = None
        self._scanned_at = -math.inf

    def read_celsius(self) -> float | None:
        now = self._clock()
        if now - self._scanned_at < self._rescan_seconds:
            if self._sensor is None:
                return None
            temperature = _read_millidegrees(self._sensor)
            if temperature is not None:
                return temperature
        self._scanned_at = now
        found = self._find_sensor()
        self._sensor = None if found is None else found[0]
        return None if found is None else found[1]

    def _find_sensor(self) -> tuple[Path, float] | None:
        candidates: list[tuple[int, str, Path, float]] = []
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
                    candidates.append((priority, str(input_path), input_path, temperature))

        if not candidates:
            return None
        _priority, _name, path, temperature = min(candidates)
        return path, temperature


class GpuTemperatureProvider:
    """GPU temperature from the drm cards' hwmon sensors (amdgpu's "edge", nouveau, xe).

    The discrete GPU (the Framework 16's graphics module) wins over the integrated one. While it
    is runtime-suspended this reads nothing and returns None: reading its sensor could wake it
    and cost battery. The chosen card is kept and found again every `rescan_seconds` or after a
    failed read, like the CPU sensor.
    """

    def __init__(
        self,
        *,
        drm_root: Path = Path("/sys/class/drm"),
        clock: Callable[[], float] = time.monotonic,
        rescan_seconds: float = SENSOR_RESCAN_SECONDS,
    ) -> None:
        self._drm_root = drm_root
        self._clock = clock
        self._rescan_seconds = rescan_seconds
        self._sensor: tuple[Path, Path] | None = None  # (runtime_status, temp*_input)
        self._scanned_at = -math.inf

    def read_celsius(self) -> float | None:
        now = self._clock()
        if now - self._scanned_at >= self._rescan_seconds:
            self._scanned_at = now
            self._sensor = self._find_sensor()
        if self._sensor is None:
            return None
        status_file, input_file = self._sensor
        if _read_text(status_file) == "suspended":
            return None
        temperature = _read_millidegrees(input_file)
        if temperature is None:
            self._scanned_at = -math.inf  # gone or failing: look again next time
        return temperature

    def _find_sensor(self) -> tuple[Path, Path] | None:
        candidates: list[tuple[int, int, str, Path, Path]] = []
        try:
            cards = sorted(path for path in self._drm_root.iterdir() if _CARD.fullmatch(path.name))
        except OSError:
            return None
        for card in cards:
            device = card / "device"
            # On a laptop the integrated GPU drives the boot display; anything else is discrete.
            integrated = 1 if _read_text(device / "boot_vga") == "1" else 0
            try:
                inputs = sorted(device.glob("hwmon/hwmon*/temp*_input"))
            except OSError:
                continue
            for input_path in inputs:
                label_path = input_path.with_name(input_path.name.replace("_input", "_label"))
                label = (_read_text(label_path) or "").casefold()
                priority = _GPU_LABELS.get(label, 2 if not label else None)
                if priority is not None:
                    status = device / "power" / "runtime_status"
                    candidates.append((integrated, priority, str(input_path), status, input_path))
        if not candidates:
            return None
        *_order, status, input_path = min(candidates)
        return status, input_path


_CARD = re.compile(r"card\d+")
_GPU_LABELS = {"edge": 0, "junction": 1, "pkg": 1, "gpu": 1}


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
