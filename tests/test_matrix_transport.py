from __future__ import annotations

import pytest

from framework_led_dashboard.matrix_transport import encode_grayscale_frame


def test_grayscale_frame_matches_official_stage_and_commit_protocol() -> None:
    frame = [[(x * 34 + y) % 256 for x in range(9)] for y in range(34)]

    commands = encode_grayscale_frame(frame)

    expected_stage_commands = tuple(
        bytes([0x32, 0xAC, 0x07, x, *[(x * 34 + y) % 256 for y in range(34)]])
        for x in range(9)
    )
    assert commands == (*expected_stage_commands, bytes([0x32, 0xAC, 0x08, 0x00]))


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        ([[0] * 9 for _ in range(33)], "34 rows"),
        ([[0] * 8 for _ in range(34)], "9 columns"),
        ([[256] * 9 for _ in range(34)], "between 0 and 255"),
        ([[True] * 9 for _ in range(34)], "between 0 and 255"),
    ],
)
def test_grayscale_frame_rejects_invalid_dimensions_and_pixels(
    frame: list[list[int]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        encode_grayscale_frame(frame)
