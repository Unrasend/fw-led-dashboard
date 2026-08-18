from __future__ import annotations

import pytest

from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH, Frame
from framework_led_dashboard.life import LIFE_HEIGHT, LIFE_WIDTH, LifeAnimation, evolve


def _life_grid(*live_cells: tuple[int, int]) -> tuple[tuple[bool, ...], ...]:
    live = set(live_cells)
    return tuple(
        tuple((x, y) in live for x in range(LIFE_WIDTH)) for y in range(LIFE_HEIGHT)
    )


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


def test_life_wraps_at_its_own_nine_by_twelve_boundaries() -> None:
    state = _life_grid((8, 11), (8, 0), (0, 11))

    next_state = evolve(state)

    assert LIFE_WIDTH == 9
    assert LIFE_HEIGHT == 12
    assert next_state[0][0] is True
    assert len(next_state) == 12
    assert all(len(row) == 9 for row in next_state)


@pytest.mark.parametrize(
    "stable_state",
    [
        _life_grid(),
        _life_grid((1, 1), (2, 1), (1, 2), (2, 2)),
    ],
)
def test_life_reseeds_after_death_or_still_state(
    stable_state: tuple[tuple[bool, ...], ...],
) -> None:
    replacement = _life_grid((4, 4), (5, 4), (4, 5))
    animation = LifeAnimation(initial_state=stable_state, seed_factory=lambda: replacement)

    assert animation.step() == replacement


def test_life_reseeds_when_a_short_oscillator_repeats() -> None:
    horizontal_blinker = _life_grid((3, 4), (4, 4), (5, 4))
    vertical_blinker = _life_grid((4, 3), (4, 4), (4, 5))
    replacement = _life_grid((1, 1), (2, 1), (1, 2))
    animation = LifeAnimation(
        initial_state=horizontal_blinker,
        seed_factory=lambda: replacement,
    )

    assert animation.step() == vertical_blinker
    assert animation.step() == replacement
