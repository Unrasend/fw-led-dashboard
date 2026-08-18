from __future__ import annotations

import math

from .battery import BatteryMode, BatteryPower
from .frame import FRAME_WIDTH, Frame
from .pixel_font import draw_text, text_width

VALUE_INTENSITY = 220
UNKNOWN_INTENSITY = 24
CPU_TEMPERATURE_TOP = 18
POWER_TOP = 27


def render_right_panel(
    *,
    cpu_temperature_c: float | None,
    battery_power: BatteryPower,
) -> Frame:
    frame = Frame()
    cpu_temperature_is_known = cpu_temperature_c is not None and math.isfinite(
        cpu_temperature_c
    )
    cpu_text = _format_cpu_temperature(cpu_temperature_c)
    _draw_centered(
        frame,
        cpu_text,
        y=CPU_TEMPERATURE_TOP,
        intensity=VALUE_INTENSITY if cpu_temperature_is_known else UNKNOWN_INTENSITY,
    )

    power_text = _format_power(battery_power)
    power_is_unknown = power_text == "--"
    _draw_centered(
        frame,
        power_text,
        y=POWER_TOP,
        intensity=UNKNOWN_INTENSITY if power_is_unknown else VALUE_INTENSITY,
    )
    return frame


def _format_cpu_temperature(temperature_c: float | None) -> str:
    if temperature_c is None or not math.isfinite(temperature_c):
        return "--"
    rounded = int(max(0.0, min(999.0, temperature_c)) + 0.5)
    if rounded < 100:
        return f"C{rounded:02d}"
    return f"{rounded:03d}"


def _format_power(power: BatteryPower) -> str:
    if power.mode is BatteryMode.CHARGING:
        return "CHG"
    if power.mode is BatteryMode.AC:
        return "AC"
    if power.mode is not BatteryMode.DISCHARGING:
        return "--"

    watts = power.watts
    if watts is None or not math.isfinite(watts) or watts < 0:
        return "--"
    return f"{min(99, int(watts + 0.5))}W"


def _draw_centered(frame: Frame, text: str, *, y: int, intensity: int) -> None:
    spacing = 0 if len(text) >= 3 else 1
    x = (FRAME_WIDTH - text_width(text, spacing=spacing)) // 2
    draw_text(frame, text, x=x, y=y, intensity=intensity, spacing=spacing)
