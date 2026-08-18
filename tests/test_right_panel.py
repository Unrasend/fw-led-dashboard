from __future__ import annotations

import pytest

from framework_led_dashboard.frame import Frame
from framework_led_dashboard.pixel_font import draw_text


def test_pixel_font_draws_three_compact_glyphs_across_nine_columns() -> None:
    frame = Frame()

    draw_text(frame, "12W", x=0, y=18, intensity=200)

    rendered = tuple(
        "".join("#" if pixel else "." for pixel in row)
        for row in frame.snapshot()[18:23]
    )
    assert rendered == (
        ".#.####.#",
        "##...##.#",
        ".#.####.#",
        ".#.#..###",
        "#######.#",
    )


def test_pixel_font_rejects_unknown_glyphs() -> None:
    with pytest.raises(ValueError, match="unsupported glyph"):
        draw_text(Frame(), "?", x=0, y=0)
