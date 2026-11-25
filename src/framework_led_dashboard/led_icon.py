"""The tray icon, drawn in plain Python so neither GTK nor cairo is needed to make it.

A dark LED-module outline with three bars of dots: amber when on, grey when off, with a red
dot when the dashboard is not running. Pixels come out as RGBA rows (for GdkPixbuf) or as
ARGB32 in network byte order (for StatusNotifierItem's IconPixmap).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

ICON_STATES = ("on", "off", "unreachable")
_BAR_HEIGHTS = (3, 6, 4)
_DOT_ROWS = 7
_SAMPLES = 4  # anti-aliasing: 4 x 4 samples per pixel
_BODY = (0.15, 0.15, 0.15)
_LIT = {"on": (1.0, 0.69, 0.23), "off": (0.6, 0.6, 0.6), "unreachable": (0.6, 0.6, 0.6)}
_UNLIT = {"on": (0.30, 0.23, 0.13), "off": (0.27, 0.27, 0.27), "unreachable": (0.27, 0.27, 0.27)}
_ALARM = (0.9, 0.28, 0.3)

Shape = tuple[tuple[float, float, float, float], Callable[[float, float], bool]]


@cache
def icon_rgba(state: str, size: int) -> bytes:
    """Straight-alpha RGBA, `size` x `size`, row by row."""
    if state not in ICON_STATES:
        raise ValueError(f"unknown icon state: {state}")
    if size < 8:
        raise ValueError("icon size must be at least 8 pixels")
    pixels = [0.0] * (size * size * 4)  # premultiplied while drawing
    scale = size / 64  # the artwork is drawn on a 64 x 64 grid
    for color, (bounds, inside) in _shapes(state):
        _paint(pixels, size, scale, bounds, inside, color)
    return bytes(_unpremultiply(pixels))


def icon_argb32(state: str, size: int) -> bytes:
    """ARGB32 in network byte order, as StatusNotifierItem's IconPixmap wants it."""
    rgba = icon_rgba(state, size)
    argb = bytearray(len(rgba))
    argb[0::4], argb[1::4], argb[2::4], argb[3::4] = rgba[3::4], rgba[0::4], rgba[1::4], rgba[2::4]
    return bytes(argb)


def _shapes(state: str) -> list[tuple[tuple[float, float, float], Shape]]:
    shapes = [(_BODY, _rounded_rectangle(12, 2, 40, 60, 9))]
    for column, height in enumerate(_BAR_HEIGHTS):
        for row in range(_DOT_ROWS):
            lit = row >= _DOT_ROWS - height
            center_y = 10.5 + row * 7 + row // 4
            color = (_LIT if lit else _UNLIT)[state]
            shapes.append((color, _circle(21.5 + column * 10, center_y, 3.5)))
    if state == "unreachable":
        shapes.append((_ALARM, _circle(48, 48, 12)))
    return shapes


def _rounded_rectangle(x: float, y: float, width: float, height: float, radius: float) -> Shape:
    def inside(px: float, py: float) -> bool:
        if not (x <= px <= x + width and y <= py <= y + height):
            return False
        cx = min(max(px, x + radius), x + width - radius)
        cy = min(max(py, y + radius), y + height - radius)
        return (px - cx) ** 2 + (py - cy) ** 2 <= radius * radius

    return (x, y, x + width, y + height), inside


def _circle(cx: float, cy: float, radius: float) -> Shape:
    def inside(px: float, py: float) -> bool:
        return (px - cx) ** 2 + (py - cy) ** 2 <= radius * radius

    return (cx - radius, cy - radius, cx + radius, cy + radius), inside


def _paint(
    pixels: list[float],
    size: int,
    scale: float,
    bounds: tuple[float, float, float, float],
    inside: Callable[[float, float], bool],
    color: tuple[float, float, float],
) -> None:
    left, top, right, bottom = bounds
    step = 1 / _SAMPLES
    for py in range(max(0, int(top * scale)), min(size, int(bottom * scale) + 1)):
        for px in range(max(0, int(left * scale)), min(size, int(right * scale) + 1)):
            hits = sum(
                inside((px + (sx + 0.5) * step) / scale, (py + (sy + 0.5) * step) / scale)
                for sx in range(_SAMPLES)
                for sy in range(_SAMPLES)
            )
            if not hits:
                continue
            alpha = hits / (_SAMPLES * _SAMPLES)
            index = (py * size + px) * 4
            for channel in range(3):  # source-over, premultiplied
                below = pixels[index + channel]
                pixels[index + channel] = color[channel] * alpha + below * (1 - alpha)
            pixels[index + 3] = alpha + pixels[index + 3] * (1 - alpha)


def _unpremultiply(pixels: list[float]) -> list[int]:
    out = []
    for index in range(0, len(pixels), 4):
        alpha = pixels[index + 3]
        if alpha <= 0:
            out.extend((0, 0, 0, 0))
            continue
        out.extend(round(min(1.0, pixels[index + channel] / alpha) * 255) for channel in range(3))
        out.append(round(alpha * 255))
    return out
