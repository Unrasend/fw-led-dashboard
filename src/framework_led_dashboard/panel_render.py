"""Draw a panel's widgets (layout.py) into one 9x34 frame."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .alerts import AlertState
from .bar_sources import BAR_SOURCES
from .equalizer import draw_equalizer
from .frame import FRAME_WIDTH, Frame
from .layout import BlinkRule, Widget, placed
from .pixel_font import GLYPH_HEIGHT, draw_text, text_width
from .system_metrics import SystemMetrics
from .weather import WeatherReading
from .weather_icons import draw_weather_icon

VALUE_INTENSITY = 220
UNKNOWN_INTENSITY = 24
BAR_COLUMNS = ((0, 1), (3, 4, 5), (7, 8))
UNKNOWN_METRICS = SystemMetrics(battery_percent=None, cpu_percent=None, ram_used_percent=None)
NO_ALERTS = AlertState()


@dataclass(frozen=True, slots=True)
class PanelData:
    """Everything a widget can show."""

    metrics: SystemMetrics = UNKNOWN_METRICS
    cpu_temperature_c: float | None = None
    gpu_temperature_c: float | None = None
    weather: WeatherReading | None = None
    alerts: AlertState = NO_ALERTS
    blinking_bars: frozenset[tuple[str, BlinkRule]] = frozenset()  # (source, rule) that hold


def render_panel(
    widgets: Sequence[Widget],
    data: PanelData,
    *,
    spectrum: Sequence[float] | None = None,
    mirrored: bool = False,
    weather_frame: int = 0,
    blink_on: bool = True,
) -> Frame:
    """`mirrored` flips the equalizer (the left module's, so the bass meets in the middle);
    `blink_on` is the visible half of the blink of a critical value."""
    frame = Frame()
    for top, widget in placed(widgets):
        if widget.kind == "weather":
            if data.weather is not None:
                draw_weather_icon(
                    frame,
                    data.weather.weather_code,
                    is_day=data.weather.is_day,
                    frame_index=weather_frame,
                    top=top,
                )
        elif widget.kind == "temperature":
            _draw_temperature(frame, widget.source, top, data, blink_on=blink_on)
        elif widget.kind == "bars":
            _draw_bars(frame, widget, top, data, blink_on=blink_on)
        elif widget.kind == "equalizer":
            draw_equalizer(frame, spectrum, mirrored=mirrored, top=top, rows=widget.height)
        elif widget.kind == "separator" and widget.style == "line":
            frame.fill_rect(x=0, y=top, width=FRAME_WIDTH, height=1, value=VALUE_INTENSITY)
    return frame


def format_temperature(temperature_c: float | None) -> str:
    """CPU and GPU: up to three digits, no sign; "--" when unknown."""
    if temperature_c is None or not math.isfinite(temperature_c):
        return "--"
    return str(int(max(0.0, min(999.0, temperature_c)) + 0.5))


def _draw_temperature(
    frame: Frame, source: str, top: int, data: PanelData, *, blink_on: bool
) -> None:
    if source == "outside":
        if not blink_on and data.alerts.outside_temperature:
            return
        if data.weather is None:
            _draw_centered(frame, "--", y=top, intensity=UNKNOWN_INTENSITY)
        else:
            _draw_outside_temperature(frame, data.weather.temperature_c, top)
        return
    if source == "cpu":
        if not blink_on and data.alerts.cpu_temperature:
            return
        value = data.cpu_temperature_c
    else:
        value = data.gpu_temperature_c
    text = format_temperature(value)
    intensity = UNKNOWN_INTENSITY if text == "--" else VALUE_INTENSITY
    _draw_centered(frame, text, y=top, intensity=intensity)


def _draw_outside_temperature(frame: Frame, temperature_c: float, top: int) -> None:
    rounded = math.floor(temperature_c + 0.5) if temperature_c >= 0 else math.ceil(temperature_c - 0.5)
    value = max(-99, min(99, rounded))
    if value <= -10:
        # Three 3-px glyphs would fill all nine columns and touch:
        # 1-px minus, 1-px gaps, no degree dot.
        frame.set_pixel(0, top + GLYPH_HEIGHT // 2, VALUE_INTENSITY)
        draw_text(frame, str(-value), x=2, y=top, intensity=VALUE_INTENSITY, spacing=1)
        return
    text = str(value)
    x = 2 if len(text) == 1 else 0
    # A gap keeps the minus from merging with a digit's middle bar (-2, -5, -9, ...).
    spacing = 1 if value < 0 else 0
    draw_text(frame, text, x=x, y=top, intensity=VALUE_INTENSITY, spacing=spacing)
    frame.set_pixel(8, top, VALUE_INTENSITY)  # the degree dot marks the outside temperature


def _draw_centered(frame: Frame, text: str, *, y: int, intensity: int) -> None:
    spacing = 0 if len(text) >= 3 else 1
    x = (FRAME_WIDTH - text_width(text, spacing=spacing)) // 2
    draw_text(frame, text, x=x, y=y, intensity=intensity, spacing=spacing)


def _draw_bars(frame: Frame, widget: Widget, top: int, data: PanelData, *, blink_on: bool) -> None:
    bottom = top + widget.height - 1
    for columns, key, rule in zip(BAR_COLUMNS, widget.bars, widget.blink, strict=True):
        if blink_on or (key, rule) not in data.blinking_bars:
            _draw_bar(frame, columns, BAR_SOURCES[key].read(data.metrics), bottom, widget.height)


def _draw_bar(
    frame: Frame, columns: tuple[int, ...], percent: float | None, bottom: int, height: int
) -> None:
    if percent is None:
        frame.fill_rect(x=columns[0], y=bottom, width=len(columns), height=1, value=UNKNOWN_INTENSITY)
        return
    clamped_percent = max(0.0, min(100.0, percent))
    filled_rows = int(clamped_percent * height / 100 + 0.5)
    if filled_rows == 0:
        return
    frame.fill_rect(
        x=columns[0],
        y=bottom - filled_rows + 1,
        width=len(columns),
        height=filled_rows,
        value=VALUE_INTENSITY,
    )
