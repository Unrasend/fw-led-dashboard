from __future__ import annotations

from .frame import FRAME_HEIGHT, FRAME_WIDTH, Frame

Glyph = tuple[str, str, str, str, str]

_GLYPHS: dict[str, Glyph] = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", ".##", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", ".#.", ".#.", ".#."),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    "A": (".#.", "#.#", "###", "#.#", "#.#"),
    "C": ("###", "#..", "#..", "#..", "###"),
    "G": ("###", "#..", "#.#", "#.#", "###"),
    "H": ("#.#", "#.#", "###", "#.#", "#.#"),
    "W": ("#.#", "#.#", "#.#", "###", "#.#"),
    "-": ("...", "...", "###", "...", "..."),
}

GLYPH_WIDTH = 3
GLYPH_HEIGHT = 5


def text_width(text: str, *, spacing: int = 0) -> int:
    if spacing < 0:
        raise ValueError("spacing must not be negative")
    if not text:
        return 0
    return len(text) * GLYPH_WIDTH + (len(text) - 1) * spacing


def draw_text(
    frame: Frame,
    text: str,
    *,
    x: int,
    y: int,
    intensity: int = 255,
    spacing: int = 0,
) -> None:
    glyphs: list[Glyph] = []
    for character in text.upper():
        try:
            glyphs.append(_GLYPHS[character])
        except KeyError as error:
            raise ValueError(f"unsupported glyph: {character!r}") from error

    width = text_width(text, spacing=spacing)
    if x < 0 or y < 0 or x + width > FRAME_WIDTH or y + GLYPH_HEIGHT > FRAME_HEIGHT:
        raise IndexError("text extends outside framebuffer")

    for glyph_index, glyph in enumerate(glyphs):
        glyph_x = x + glyph_index * (GLYPH_WIDTH + spacing)
        for row_index, row in enumerate(glyph):
            for column_index, pixel in enumerate(row):
                if pixel == "#":
                    frame.set_pixel(glyph_x + column_index, y + row_index, intensity)
