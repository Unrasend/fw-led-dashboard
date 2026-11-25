from __future__ import annotations

from collections.abc import Sequence

from .frame import FRAME_WIDTH, Frame

EQUALIZER_TOP = 23
EQUALIZER_ROWS = 11
EQUALIZER_BOTTOM = EQUALIZER_TOP + EQUALIZER_ROWS - 1
BODY_INTENSITY = 128
PEAK_INTENSITY = 220


def equalizer_bar_heights(levels: Sequence[float], rows: int = EQUALIZER_ROWS) -> tuple[int, ...]:
    if len(levels) != FRAME_WIDTH:
        raise ValueError(f"equalizer expects {FRAME_WIDTH} band levels, got {len(levels)}")
    return tuple(int(max(0.0, min(1.0, level)) * rows + 0.5) for level in levels)


def draw_equalizer(
    frame: Frame,
    levels: Sequence[float] | None,
    *,
    mirrored: bool = False,
    top: int = EQUALIZER_TOP,
    rows: int = EQUALIZER_ROWS,
) -> None:
    """Bars growing up from the bottom row of `rows` rows starting at `top`. Mirrored puts the
    lowest band on the right, so on the left module the bass sits next to the right one's."""
    if levels is None:
        return
    bottom = top + rows - 1
    heights = equalizer_bar_heights(levels, rows)
    for x, height in enumerate(reversed(heights) if mirrored else heights):
        for step in range(height):
            intensity = PEAK_INTENSITY if step == height - 1 else BODY_INTENSITY
            frame.set_pixel(x, bottom - step, intensity)
