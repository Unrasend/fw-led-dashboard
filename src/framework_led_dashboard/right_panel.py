from __future__ import annotations

import math

from .battery import BatteryMode, BatteryPower
from .frame import FRAME_WIDTH, Frame
from .pixel_font import draw_text, text_width
from .weather import WeatherReading
from .weather_icons import draw_weather_icon

VALUE_INTENSITY = 220
UNKNOWN_INTENSITY = 24
OUTSIDE_TEMPERATURE_TOP = 9
CPU_TEMPERATURE_TOP = 18
POWER_TOP = 27


def render_right_panel(
    *,
    cpu_temperature_c: float | None,
    battery_power: BatteryPower,
    weather: WeatherReading | None = None,
    weather_frame: int = 0,
) -> Frame:
    frame = Frame()
    if weather is None:
        _draw_centered(
            frame,
            "--",
            y=OUTSIDE_TEMPERATURE_TOP,
            intensity=UNKNOWN_INTENSITY,
        )
    else:
        draw_weather_icon(frame, weather.weather_code, frame_index=weather_frame)
        _draw_outside_temperature(frame, weather.temperature_c)

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


def _draw_outside_temperature(frame: Frame, temperature_c: float) -> None:
    rounded = math.floor(temperature_c + 0.5) if temperature_c >= 0 else math.ceil(
        temperature_c - 0.5
    )
    text = str(max(-99, min(99, rounded)))
    if len(text) <= 2:
        x = 0 if len(text) == 2 else 2
        draw_text(
            frame,
            text,
            x=x,
            y=OUTSIDE_TEMPERATURE_TOP,
            intensity=VALUE_INTENSITY,
        )
        frame.set_pixel(8, OUTSIDE_TEMPERATURE_TOP, VALUE_INTENSITY)
        return
    draw_text(
        frame,
        text,
        x=0,
        y=OUTSIDE_TEMPERATURE_TOP,
        intensity=VALUE_INTENSITY,
    )


def _draw_centered(frame: Frame, text: str, *, y: int, intensity: int) -> None:
    spacing = 0 if len(text) >= 3 else 1
    x = (FRAME_WIDTH - text_width(text, spacing=spacing)) // 2
    draw_text(frame, text, x=x, y=y, intensity=intensity, spacing=spacing)
