"""What each of the left panel's three bars can show, with the tray's labels and hints."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .alerts import AlertState
from .system_metrics import SystemMetrics

BAR_COUNT = 3
BAR_SLOT_NAMES = ("Bar 1 (left)", "Bar 2 (middle)", "Bar 3 (right)")


def _never(_alerts: AlertState) -> bool:
    return False


@dataclass(frozen=True, slots=True)
class BarSource:
    key: str
    label: str
    hint: str
    read: Callable[[SystemMetrics], float | None]
    critical: Callable[[AlertState], bool] = _never


BAR_SOURCES: dict[str, BarSource] = {
    source.key: source
    for source in (
        BarSource(
            "battery",
            "Battery",
            "Battery charge, %.\nSource: /sys/class/power_supply/BAT1/capacity\n"
            "Blinks while critical ([alerts] battery_* in config.toml).",
            lambda metrics: metrics.battery_percent,
            lambda alerts: alerts.battery,
        ),
        BarSource(
            "cpu",
            "CPU",
            "CPU load across all cores, %.\nSource: /proc/stat, the difference between\n"
            "two samples taken one second apart.",
            lambda metrics: metrics.cpu_percent,
            lambda alerts: alerts.cpu_usage,
        ),
        BarSource(
            "ram",
            "RAM",
            "Memory in use, %.\nSource: /proc/meminfo, MemTotal − MemAvailable\n"
            "Blinks while critical ([alerts] ram_* in config.toml).",
            lambda metrics: metrics.ram_used_percent,
            lambda alerts: alerts.ram,
        ),
        BarSource(
            "gpu",
            "GPU",
            "GPU load, %.\nSource: /sys/class/drm/card*/device/gpu_busy_percent (amdgpu),\n"
            "the busiest awake card. A sleeping GPU is never polled, so it is not woken up.",
            lambda metrics: metrics.gpu_percent,
        ),
        BarSource(
            "swap",
            "Swap",
            "Swap in use, %.\nSource: /proc/meminfo, SwapTotal − SwapFree",
            lambda metrics: metrics.swap_used_percent,
        ),
        BarSource(
            "disk",
            "Disk /",
            'Space used on the root partition, %.\nSource: statvfs("/"), same as df.',
            lambda metrics: metrics.disk_used_percent,
        ),
    )
}

DEFAULT_BARS: tuple[str, ...] = ("battery", "cpu", "ram")


def validate_bars(bars: Sequence[str]) -> tuple[str, ...]:
    chosen = tuple(bars)
    if len(chosen) != BAR_COUNT:
        raise ValueError(f"bars must name exactly {BAR_COUNT} sources")
    unknown = [key for key in chosen if key not in BAR_SOURCES]
    if unknown:
        raise ValueError(
            f"bars has unknown sources: {', '.join(map(str, unknown))} "
            f"(known: {', '.join(BAR_SOURCES)})"
        )
    return chosen
