"""Render the frames of the demo page (docs/) with the dashboard's own renderers.

    PYTHONPATH=src python3 scripts/build_demo_data.py

rewrites docs/demo-data.js; tests/test_demo_page.py fails while it is out of date.

The page shows the default layout (layout.DEFAULT_LAYOUT) and composes each panel from layers
that never overlap on the hardware either: the right panel is its text (GPU and CPU
temperature) plus the weather icon in rows 0-9, both panels add the equalizer in rows 23-33.
docs/panel.js draws the equalizer with the same rule as equalizer.py; the test checks the
composition against the real renderers.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from framework_led_dashboard import equalizer
from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from framework_led_dashboard.layout import DEFAULT_LAYOUT, placed
from framework_led_dashboard.panel_render import PanelData, render_panel
from framework_led_dashboard.scheduler import weather_frame_index
from framework_led_dashboard.system_metrics import SystemMetrics
from framework_led_dashboard.weather import WeatherReading
from framework_led_dashboard.weather_codes import SUPPORTED_WEATHER_CODES, classify_weather_code
from framework_led_dashboard.weather_icons import (
    ICON_FRAMES,
    ICON_HEIGHT,
    ICON_INTENSITIES,
    ICON_WIDTH,
    weather_icon,
)

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "demo-data.js"
PIXEL_CHARS = {0: ".", 24: "u", **{value: char for char, value in ICON_INTENSITIES.items()}}
TIMELINE_STEPS = 40  # one sensor sample per second, then it loops
OUTSIDE_TEMPERATURE_C = 12.0
PANEL_FRAME_SECONDS = 0.15  # what the hardware manages in grayscale: about 6-7 FPS

# Open-Meteo's names for the WMO codes, shortened to fit a card.
LABELS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light rain showers",
    81: "Rain showers",
    82: "Violent rain showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm, hail",
    99: "Thunderstorm, heavy hail",
}


def encode(pixels: FramePixels) -> str:
    return "".join(PIXEL_CHARS[value] for row in pixels for value in row)


def icon_frames(code: int, *, is_day: bool) -> list[str]:
    symbol = classify_weather_code(code)
    frames = []
    for index in range(ICON_FRAMES):
        rows = weather_icon(symbol, is_day=is_day, frame_index=index)
        frames.append(
            "".join(PIXEL_CHARS[ICON_INTENSITIES.get(pixel, 0)] for row in rows for pixel in row)
        )
    return frames


def metrics_at(step: int) -> tuple[SystemMetrics, float]:
    """A made-up but plausible minute of work: CPU load waves, the temperature follows."""

    def wave(at: int) -> float:
        phase = 2 * math.pi * at / TIMELINE_STEPS
        waves = 13 * math.sin(phase) + 8 * math.sin(3 * phase + 0.7) + 5 * math.sin(7 * phase + 2.1)
        return 24 + waves

    cpu = max(3.0, wave(step))
    temperature = 41 + 0.45 * max(3.0, wave(step - 2))
    ram = 46 + 2 * math.sin(2 * math.pi * step / TIMELINE_STEPS)
    return SystemMetrics(battery_percent=78, cpu_percent=cpu, ram_used_percent=ram), temperature


def gpu_temperature_at(step: int) -> float:
    """The GPU warms up and cools down more slowly than the CPU."""
    return 46 + 6 * math.sin(2 * math.pi * (step - 6) / TIMELINE_STEPS)


def panel_data(step: int, *, weather_code: int = 0, is_day: bool = True) -> PanelData:
    metrics, cpu_temperature = metrics_at(step)
    return PanelData(
        metrics=metrics,
        cpu_temperature_c=cpu_temperature,
        gpu_temperature_c=gpu_temperature_at(step),
        weather=WeatherReading(
            temperature_c=OUTSIDE_TEMPERATURE_C, weather_code=weather_code, is_day=is_day
        ),
    )


def left_base(step: int) -> FramePixels:
    """The left panel without its equalizer: outside temperature, bars and their line."""
    return render_panel(DEFAULT_LAYOUT.left, panel_data(step)).snapshot()


def right_text_layer(step: int) -> FramePixels:
    full = render_panel(DEFAULT_LAYOUT.right, panel_data(step)).snapshot()
    blank = tuple(0 for _ in range(FRAME_WIDTH))
    return tuple(blank if y < ICON_HEIGHT else row for y, row in enumerate(full))


def _equalizer_rows() -> tuple[int, int]:
    """(top, rows) of the equalizer, which the page draws itself: the same on both panels."""
    spots = {
        (top, widget.height)
        for side in (DEFAULT_LAYOUT.left, DEFAULT_LAYOUT.right)
        for top, widget in placed(side)
        if widget.kind == "equalizer"
    }
    if len(spots) != 1:
        raise ValueError("the demo needs the equalizer in the same rows on both panels")
    return spots.pop()


def build() -> dict[str, object]:
    if set(LABELS) != SUPPORTED_WEATHER_CODES:
        raise ValueError("every supported weather code needs a label")
    left, right = [], []
    for step in range(TIMELINE_STEPS):
        left.append(encode(left_base(step)))
        right.append(encode(right_text_layer(step)))
    equalizer_top, equalizer_rows = _equalizer_rows()
    return {
        "frameWidth": FRAME_WIDTH,
        "frameHeight": FRAME_HEIGHT,
        "iconWidth": ICON_WIDTH,
        "iconHeight": ICON_HEIGHT,
        "pixelValues": {char: value for value, char in PIXEL_CHARS.items()},
        "equalizer": {
            "top": equalizer_top,
            "rows": equalizer_rows,
            "body": equalizer.BODY_INTENSITY,
            "peak": equalizer.PEAK_INTENSITY,
        },
        "panelFrameSeconds": PANEL_FRAME_SECONDS,
        "weatherFramesPerSecond": weather_frame_index(1.0),
        "timeline": {"stepSeconds": 1, "left": left, "right": right},
        "icons": [
            {
                "code": code,
                "label": LABELS[code],
                "kind": classify_weather_code(code).kind.value,
                "day": icon_frames(code, is_day=True),
                "night": icon_frames(code, is_day=False),
            }
            for code in sorted(SUPPORTED_WEATHER_CODES)
        ],
    }


def render_module(data: dict[str, object]) -> str:
    return (
        "// Generated by scripts/build_demo_data.py from the dashboard's renderers. Do not edit.\n"
        f"export const DEMO = {json.dumps(data, indent=1)};\n"
    )


def main() -> None:
    OUTPUT.write_text(render_module(build()), encoding="utf-8")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
