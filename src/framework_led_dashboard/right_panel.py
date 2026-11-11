from __future__ import annotations

import math
from collections.abc import Sequence

from .alerts import AlertState
from .equalizer import draw_equalizer
from .frame import FRAME_WIDTH, Frame
from .pixel_font import GLYPH_HEIGHT, draw_text, text_width
from .weather import WeatherReading
from .weather_icons import draw_weather_icon

VALUE_INTENSITY = 220
UNKNOWN_INTENSITY = 24
OUTSIDE_TEMPERATURE_TOP = 11
CPU_TEMPERATURE_TOP = 17


def render_right_panel(
    *,
    cpu_temperature_c: float | None,
    weather: WeatherReading | None = None,
    weather_frame: int = 0,
    spectrum: Sequence[float] | None = None,
    alerts: AlertState | None = None,
    blink_on: bool = True,
) -> Frame:
    alerts = alerts or AlertState()
    frame = Frame()
    if weather is None:
        if blink_on or not alerts.outside_temperature:
            _draw_centered(frame, "--", y=OUTSIDE_TEMPERATURE_TOP, intensity=UNKNOWN_INTENSITY)
    else:
        draw_weather_icon(frame, weather.weather_code, is_day=weather.is_day, frame_index=weather_frame)
        if blink_on or not alerts.outside_temperature:
            _draw_outside_temperature(frame, weather.temperature_c)

    cpu_text = _format_cpu_temperature(cpu_temperature_c)
    if blink_on or not alerts.cpu_temperature:
        intensity = UNKNOWN_INTENSITY if cpu_text == "--" else VALUE_INTENSITY
        _draw_centered(frame, cpu_text, y=CPU_TEMPERATURE_TOP, intensity=intensity)
    draw_equalizer(frame, spectrum)
    return frame


def _format_cpu_temperature(temperature_c: float | None) -> str:
    if temperature_c is None or not math.isfinite(temperature_c):
        return "--"
    return str(int(max(0.0, min(999.0, temperature_c)) + 0.5))


def _draw_outside_temperature(frame: Frame, temperature_c: float) -> None:
    rounded = math.floor(temperature_c + 0.5) if temperature_c >= 0 else math.ceil(temperature_c - 0.5)
    value = max(-99, min(99, rounded))
    if value <= -10:
        # Three 3-px glyphs would fill all nine columns and touch:
        # 1-px minus, 1-px gaps, no degree dot.
        frame.set_pixel(0, OUTSIDE_TEMPERATURE_TOP + GLYPH_HEIGHT // 2, VALUE_INTENSITY)
        draw_text(
            frame, str(-value), x=2, y=OUTSIDE_TEMPERATURE_TOP, intensity=VALUE_INTENSITY, spacing=1
        )
        return
    text = str(value)
    x = 2 if len(text) == 1 else 0
    # A gap keeps the minus from merging with a digit's middle bar (-2, -5, -9, ...).
    spacing = 1 if value < 0 else 0
    draw_text(
        frame, text, x=x, y=OUTSIDE_TEMPERATURE_TOP, intensity=VALUE_INTENSITY, spacing=spacing
    )
    frame.set_pixel(8, OUTSIDE_TEMPERATURE_TOP, VALUE_INTENSITY)


def _draw_centered(frame: Frame, text: str, *, y: int, intensity: int) -> None:
    spacing = 0 if len(text) >= 3 else 1
    x = (FRAME_WIDTH - text_width(text, spacing=spacing)) // 2
    draw_text(frame, text, x=x, y=y, intensity=intensity, spacing=spacing)
