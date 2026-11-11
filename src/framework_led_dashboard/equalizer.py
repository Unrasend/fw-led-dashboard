from __future__ import annotations

from collections.abc import Sequence

from .frame import FRAME_WIDTH, Frame

EQUALIZER_TOP = 23
EQUALIZER_ROWS = 11
EQUALIZER_BOTTOM = EQUALIZER_TOP + EQUALIZER_ROWS - 1
BODY_INTENSITY = 128
PEAK_INTENSITY = 220


def equalizer_bar_heights(levels: Sequence[float]) -> tuple[int, ...]:
    if len(levels) != FRAME_WIDTH:
        raise ValueError(f"equalizer expects {FRAME_WIDTH} band levels, got {len(levels)}")
    return tuple(int(max(0.0, min(1.0, level)) * EQUALIZER_ROWS + 0.5) for level in levels)


def draw_equalizer(frame: Frame, levels: Sequence[float] | None, *, mirrored: bool = False) -> None:
    if levels is None:
        return
    heights = equalizer_bar_heights(levels)
    for x, height in enumerate(reversed(heights) if mirrored else heights):
        for step in range(height):
            intensity = PEAK_INTENSITY if step == height - 1 else BODY_INTENSITY
            frame.set_pixel(x, EQUALIZER_BOTTOM - step, intensity)
