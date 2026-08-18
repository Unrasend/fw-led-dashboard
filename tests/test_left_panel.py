from __future__ import annotations

import pytest

from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH, Frame


def test_frame_is_exactly_nine_by_thirty_four_and_starts_dark() -> None:
    frame = Frame()

    pixels = frame.snapshot()
    assert FRAME_WIDTH == 9
    assert FRAME_HEIGHT == 34
    assert len(pixels) == 34
    assert all(len(row) == 9 for row in pixels)
    assert all(pixel == 0 for row in pixels for pixel in row)


def test_frame_clamps_pixel_values_to_grayscale_byte_range() -> None:
    frame = Frame()

    frame.set_pixel(0, 0, -10)
    frame.set_pixel(1, 0, 128)
    frame.set_pixel(2, 0, 999)

    assert frame.snapshot()[0][:3] == (0, 128, 255)


def test_frame_fills_rectangles_without_touching_surrounding_pixels() -> None:
    frame = Frame()

    frame.fill_rect(x=2, y=3, width=2, height=3, value=80)

    lit_pixels = {
        (x, y)
        for y, row in enumerate(frame.snapshot())
        for x, value in enumerate(row)
        if value > 0
    }
    assert lit_pixels == {(2, 3), (3, 3), (2, 4), (3, 4), (2, 5), (3, 5)}


@pytest.mark.parametrize(
    ("x", "y"),
    [(-1, 0), (0, -1), (FRAME_WIDTH, 0), (0, FRAME_HEIGHT)],
)
def test_frame_rejects_out_of_bounds_pixels(x: int, y: int) -> None:
    with pytest.raises(IndexError, match="outside"):
        Frame().set_pixel(x, y, 1)
