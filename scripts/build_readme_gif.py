"""Render docs/preview.gif, the animated picture of both panels at the top of the README.

    PYTHONPATH=src python3 scripts/build_readme_gif.py

Needs pycairo and Pillow. The pixels come from the dashboard's renderers, like the demo
page; the equalizer plays a made-up song that repeats exactly, so the GIF loops without a
jump. Text uses the system's default sans-serif font, so the file differs between machines.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import build_demo_data
import cairo
from PIL import Image

from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from framework_led_dashboard.left_panel import render_left_panel
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.weather import WeatherReading

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "preview.gif"

LOOP_SECONDS = 10  # the weather icons repeat every 20 frames at 2 frames a second
FRAMES_PER_SECOND = 6  # about what the hardware shows in grayscale
FRAME_DELAYS_MS = (170, 160, 170)  # GIF delays are in 10 ms steps: three frames = 500 ms
WEATHER_CODE = 81  # rain showers by day: sun, cloud and rain all move
BEAT_SECONDS = 0.5  # 20 beats per loop
WINDOW_SECONDS = 1 / 36  # six analysis windows per GIF frame, close to audio.py's 32 ms

COLORS = 64
PITCH = 13
PAD = 8
MARGIN = 28
CALLOUT_WIDTH = 150
CALLOUT_GAP = 16
PANEL_GAP = 40
PANEL_WIDTH = FRAME_WIDTH * PITCH + 2 * PAD
PANEL_HEIGHT = FRAME_HEIGHT * PITCH + 2 * PAD
WIDTH = 2 * (MARGIN + CALLOUT_WIDTH + CALLOUT_GAP + PANEL_WIDTH) + PANEL_GAP
HEIGHT = 2 * MARGIN + PANEL_HEIGHT
LEFT_X = MARGIN + CALLOUT_WIDTH + CALLOUT_GAP
RIGHT_X = LEFT_X + PANEL_WIDTH + PANEL_GAP

BACKGROUND = (0x0D / 255, 0x0E / 255, 0x10 / 255)
MODULE = (0x08 / 255, 0x09 / 255, 0x0A / 255)
MODULE_EDGE = (0x24 / 255, 0x27 / 255, 0x2B / 255)
MUTED = (0x9D / 255, 0x9A / 255, 0x94 / 255)
LED = (1.0, 247 / 255, 232 / 255)

LEFT_CALLOUTS = ((10, ("battery · CPU · RAM",)), (28, ("microphone", "equalizer")))
RIGHT_CALLOUTS = (
    (4.5, ("weather, animated",)),
    (13, ("outside °",)),
    (19, ("CPU °C",)),
    (28, ("equalizer,", "mirrored")),
)


class LoopingSong:
    """The demo page's made-up song, with every rhythm a divisor of the loop."""

    def __init__(self) -> None:
        windows = round(LOOP_SECONDS / WINDOW_SECONDS)
        rng = random.Random(16)
        self._jitter = [
            [0.12 * (rng.random() - 0.5) for _ in range(FRAME_WIDTH)] for _ in range(windows)
        ]
        self._levels = [0.0] * FRAME_WIDTH
        self._window = 0

    def advance(self, seconds: float) -> tuple[float, ...]:
        for _ in range(round(seconds / WINDOW_SECONDS)):
            time = self._window * WINDOW_SECONDS
            jitter = self._jitter[self._window % len(self._jitter)]
            for band, level in enumerate(self._levels):
                target = max(0.0, min(1.0, self._target(band, time) + jitter[band]))
                rate = 0.6 if target > level else 0.25
                self._levels[band] = level + rate * (target - level)
            self._window += 1
        return tuple(self._levels)

    @staticmethod
    def _target(band: int, time: float) -> float:
        loop = 2 * math.pi * (time % LOOP_SECONDS) / LOOP_SECONDS
        beat = (time % LOOP_SECONDS) / BEAT_SECONDS
        if int(beat) // 4 == 4 and beat % 4 >= 2:
            return 0.0  # a breath in the last bar
        accent = 1.2 if int(beat) % 4 == 0 else 1.0
        kick = accent * math.exp(-(beat % 1) * 2.5)
        hat = math.exp(-((beat + 0.5) % 1) * 6)
        if band <= 1:
            return 0.3 + 0.8 * kick - band * 0.08
        if band <= 3:
            return 0.3 + 0.3 * math.sin(3 * loop + band) + 0.4 * kick
        if band <= 6:
            melody = 0.5 + 0.5 * math.sin(4 * loop + band * 1.9) * math.sin(loop)
            return 0.12 + 0.8 * melody
        return 0.1 + 0.75 * hat - (band - 7) * 0.12


def panels_at(index: int, levels: tuple[float, ...]) -> tuple[FramePixels, FramePixels]:
    # Ten one-second sensor samples spread over the demo's 40-sample wave: it loops too.
    second = index // FRAMES_PER_SECOND
    step = second * build_demo_data.TIMELINE_STEPS // LOOP_SECONDS
    metrics, cpu_temperature = build_demo_data.metrics_at(step)
    left = render_left_panel(metrics, spectrum=levels).snapshot()
    right = render_right_panel(
        cpu_temperature_c=cpu_temperature,
        weather=WeatherReading(
            temperature_c=build_demo_data.OUTSIDE_TEMPERATURE_C,
            weather_code=WEATHER_CODE,
            is_day=True,
        ),
        weather_frame=index * 2 // FRAMES_PER_SECOND,
        spectrum=levels,
    ).snapshot()
    return left, right


def _rounded_rectangle(
    context: cairo.Context, x: float, y: float, w: float, h: float, r: float
) -> None:
    context.new_sub_path()
    context.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    context.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    context.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    context.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    context.close_path()


def _draw_panel(context: cairo.Context, x0: float, pixels: FramePixels) -> None:
    _rounded_rectangle(context, x0 + 0.5, MARGIN + 0.5, PANEL_WIDTH - 1, PANEL_HEIGHT - 1, 10)
    context.set_source_rgb(*MODULE)
    context.fill_preserve()
    context.set_source_rgb(*MODULE_EDGE)
    context.set_line_width(1)
    context.stroke()
    for y, row in enumerate(pixels):
        for x, value in enumerate(row):
            cx = x0 + PAD + (x + 0.5) * PITCH
            cy = MARGIN + PAD + (y + 0.5) * PITCH
            if not value:
                context.set_source_rgba(1, 1, 1, 0.055)
                context.arc(cx, cy, PITCH * 0.26, 0, 2 * math.pi)
                context.fill()
                continue
            alpha = (value / 255) ** 0.75
            glow = cairo.RadialGradient(cx, cy, 0, cx, cy, PITCH * 1.2)
            glow.add_color_stop_rgba(0, *LED, 0.55 * alpha)
            glow.add_color_stop_rgba(0.3, *LED, 0.16 * alpha)
            glow.add_color_stop_rgba(1, *LED, 0)
            context.set_source(glow)
            context.arc(cx, cy, PITCH * 1.2, 0, 2 * math.pi)
            context.fill()
            context.set_source_rgba(*LED, alpha)
            context.arc(cx, cy, PITCH * 0.3, 0, 2 * math.pi)
            context.fill()


def _draw_callouts(context: cairo.Context, callouts: tuple, *, right_side: bool) -> None:
    context.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    context.set_font_size(13)
    line_height = 16
    for row, lines in callouts:
        center = MARGIN + PAD + (row + 0.5) * PITCH
        line_x = RIGHT_X + PANEL_WIDTH + 4 if right_side else LEFT_X - 16
        context.set_source_rgba(*MUTED, 0.6)
        context.move_to(line_x, center + 0.5)
        context.line_to(line_x + 12, center + 0.5)
        context.set_line_width(1)
        context.stroke()
        context.set_source_rgb(*MUTED)
        top = center - line_height * len(lines) / 2 + 12
        for index, text in enumerate(lines):
            width = context.text_extents(text).x_advance
            x = line_x + 20 if right_side else line_x - 8 - width
            context.move_to(round(x), round(top + index * line_height))
            context.show_text(text)


def render_frame(left: FramePixels, right: FramePixels) -> Image.Image:
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, WIDTH, HEIGHT)
    context = cairo.Context(surface)
    context.set_source_rgb(*BACKGROUND)
    context.paint()
    _draw_callouts(context, LEFT_CALLOUTS, right_side=False)
    _draw_callouts(context, RIGHT_CALLOUTS, right_side=True)
    _draw_panel(context, LEFT_X, left)
    _draw_panel(context, RIGHT_X, right)
    surface.flush()
    return Image.frombuffer("RGB", (WIDTH, HEIGHT), bytes(surface.get_data()), "raw", "BGRX", 0, 1)


def build_frames() -> list[Image.Image]:
    song = LoopingSong()
    song.advance(LOOP_SECONDS)  # settle into the loop, so the last frame leads into the first
    frames = []
    for index in range(LOOP_SECONDS * FRAMES_PER_SECOND):
        levels = song.advance(1 / FRAMES_PER_SECOND)
        frames.append(render_frame(*panels_at(index, levels)))
    return frames


def main() -> None:
    frames = build_frames()
    # One shared palette without dithering: steady colours and small frame-to-frame deltas.
    sample = Image.new("RGB", (WIDTH, HEIGHT * 4))
    for slot, frame in enumerate(frames[:: len(frames) // 4][:4]):
        sample.paste(frame, (0, HEIGHT * slot))
    palette = sample.quantize(
        colors=COLORS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    indexed[0].save(
        OUTPUT,
        save_all=True,
        append_images=indexed[1:],
        duration=[FRAME_DELAYS_MS[index % 3] for index in range(len(indexed))],
        loop=0,
        optimize=True,
    )
    print(f"wrote {OUTPUT}: {len(indexed)} frames, {OUTPUT.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
