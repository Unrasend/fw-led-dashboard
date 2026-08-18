from __future__ import annotations


FRAME_WIDTH = 9
FRAME_HEIGHT = 34
FramePixels = tuple[tuple[int, ...], ...]


class Frame:
    """Mutable grayscale framebuffer with immutable snapshots."""

    def __init__(self) -> None:
        self._pixels = [[0 for _ in range(FRAME_WIDTH)] for _ in range(FRAME_HEIGHT)]

    def set_pixel(self, x: int, y: int, value: int) -> None:
        self._require_pixel(x, y)
        self._pixels[y][x] = max(0, min(255, int(value)))

    def fill_rect(self, *, x: int, y: int, width: int, height: int, value: int) -> None:
        if width < 0 or height < 0:
            raise ValueError("rectangle dimensions must not be negative")
        if x < 0 or y < 0 or x + width > FRAME_WIDTH or y + height > FRAME_HEIGHT:
            raise IndexError("rectangle extends outside framebuffer")

        clamped_value = max(0, min(255, int(value)))
        for pixel_y in range(y, y + height):
            for pixel_x in range(x, x + width):
                self._pixels[pixel_y][pixel_x] = clamped_value

    def snapshot(self) -> FramePixels:
        return tuple(tuple(row) for row in self._pixels)

    @staticmethod
    def _require_pixel(x: int, y: int) -> None:
        if not 0 <= x < FRAME_WIDTH or not 0 <= y < FRAME_HEIGHT:
            raise IndexError(f"pixel ({x}, {y}) is outside framebuffer")
