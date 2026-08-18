from __future__ import annotations

from collections.abc import Sequence


FRAME_WIDTH = 9
FRAME_HEIGHT = 34
_FRAMEWORK_MAGIC = bytes([0x32, 0xAC])
_STAGE_GREYSCALE_COLUMN = 0x07
_DRAW_GREYSCALE_BUFFER = 0x08


def encode_grayscale_frame(frame: Sequence[Sequence[int]]) -> tuple[bytes, ...]:
    """Encode a row-major 9x34 frame as one atomic Framework matrix update."""
    if len(frame) != FRAME_HEIGHT:
        raise ValueError(f"grayscale frame must have {FRAME_HEIGHT} rows")

    for row in frame:
        if len(row) != FRAME_WIDTH:
            raise ValueError(f"each grayscale frame row must have {FRAME_WIDTH} columns")
        for pixel in row:
            if isinstance(pixel, bool) or not isinstance(pixel, int) or not 0 <= pixel <= 255:
                raise ValueError("grayscale pixels must be integers between 0 and 255")

    stage_commands = tuple(
        _FRAMEWORK_MAGIC
        + bytes([_STAGE_GREYSCALE_COLUMN, x])
        + bytes(frame[y][x] for y in range(FRAME_HEIGHT))
        for x in range(FRAME_WIDTH)
    )
    commit_command = _FRAMEWORK_MAGIC + bytes([_DRAW_GREYSCALE_BUFFER, 0x00])
    return (*stage_commands, commit_command)
