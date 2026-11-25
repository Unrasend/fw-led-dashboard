"""What each panel shows: a stack of widgets from top to bottom.

A module is only 9 LEDs wide, so every widget takes the full width and a panel's layout is just
the order and height of its widgets. The settings window edits it; the dashboard keeps it in
display.json next to the other live settings.

Widgets (JSON form in brackets):
  weather      the animated weather icon, 10 rows          {"kind": "weather"}
  temperature  a number, 5 rows; source cpu, gpu, outside  {"kind": "temperature", "source": "cpu"}
  bars         three vertical bars, resizable              {"kind": "bars", "bars": [...], "height": 15}
  equalizer    the sound spectrum, resizable               {"kind": "equalizer", "height": 11}
  separator    a lit line (1 row) or empty space           {"kind": "separator", "style": "line"}
                                                            {"kind": "separator", "style": "space", "height": 1}
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from .bar_sources import DEFAULT_BARS, validate_bars
from .frame import FRAME_HEIGHT
from .pixel_font import GLYPH_HEIGHT
from .weather_icons import ICON_HEIGHT

WIDGET_KINDS = ("weather", "temperature", "bars", "equalizer", "separator")
TEMPERATURE_SOURCES = ("cpu", "gpu", "outside")
SEPARATOR_STYLES = ("line", "space")
SIDES = ("left", "right")

WEATHER_HEIGHT = ICON_HEIGHT
TEMPERATURE_HEIGHT = GLYPH_HEIGHT
LINE_HEIGHT = 1
MIN_BARS_HEIGHT = 3
MIN_EQUALIZER_HEIGHT = 3
MIN_SPACE_HEIGHT = 1
MAX_WIDGETS_PER_PANEL = 16

_FIXED_HEIGHTS = {"weather": WEATHER_HEIGHT, "temperature": TEMPERATURE_HEIGHT}
_MIN_HEIGHTS = {"bars": MIN_BARS_HEIGHT, "equalizer": MIN_EQUALIZER_HEIGHT}


@dataclass(frozen=True, slots=True)
class Widget:
    """One widget. Only the fields of its kind are used; the others keep their defaults."""

    kind: str
    height: int
    source: str = ""  # temperature
    bars: tuple[str, ...] = ()  # bars
    style: str = ""  # separator

    def __post_init__(self) -> None:
        if self.kind not in WIDGET_KINDS:
            raise ValueError(f"unknown widget {self.kind!r} (known: {', '.join(WIDGET_KINDS)})")
        if isinstance(self.height, bool) or not isinstance(self.height, int):
            raise TypeError(f"{self.kind} height must be a whole number of rows")
        if self.kind == "temperature" and self.source not in TEMPERATURE_SOURCES:
            raise ValueError(
                f"temperature source must be one of {', '.join(TEMPERATURE_SOURCES)}, "
                f"got {self.source!r}"
            )
        if self.kind == "bars":
            if not isinstance(self.bars, tuple):
                raise TypeError("bars must be a tuple of source names")
            validate_bars(self.bars)
        if self.kind == "separator" and self.style not in SEPARATOR_STYLES:
            raise ValueError(
                f"separator style must be one of {', '.join(SEPARATOR_STYLES)}, got {self.style!r}"
            )
        low, high = self.height_range
        if not low <= self.height <= high:
            rows = f"{low} rows" if low == high else f"{low} to {high} rows"
            raise ValueError(f"{self.label.lower()} must be {rows} high, got {self.height}")

    @property
    def height_range(self) -> tuple[int, int]:
        """Smallest and largest height this widget can have."""
        if self.kind in _FIXED_HEIGHTS:
            fixed = _FIXED_HEIGHTS[self.kind]
            return fixed, fixed
        if self.kind == "separator":
            if self.style == "line":
                return LINE_HEIGHT, LINE_HEIGHT
            return MIN_SPACE_HEIGHT, FRAME_HEIGHT
        return _MIN_HEIGHTS[self.kind], FRAME_HEIGHT

    @property
    def resizable(self) -> bool:
        low, high = self.height_range
        return low != high

    @property
    def label(self) -> str:
        if self.kind == "temperature":
            return {"cpu": "CPU temperature", "gpu": "GPU temperature"}.get(
                self.source, "Outside temperature"
            )
        if self.kind == "separator":
            return "Line" if self.style == "line" else "Space"
        return {"weather": "Weather icon", "bars": "Bars", "equalizer": "Equalizer"}[self.kind]

    def to_json(self) -> dict[str, object]:
        if self.kind == "temperature":
            return {"kind": self.kind, "source": self.source}
        if self.kind == "bars":
            return {"kind": self.kind, "bars": list(self.bars), "height": self.height}
        if self.kind == "equalizer":
            return {"kind": self.kind, "height": self.height}
        if self.kind == "separator":
            if self.style == "line":
                return {"kind": self.kind, "style": self.style}
            return {"kind": self.kind, "style": self.style, "height": self.height}
        return {"kind": self.kind}


def weather() -> Widget:
    return Widget("weather", WEATHER_HEIGHT)


def temperature(source: str) -> Widget:
    return Widget("temperature", TEMPERATURE_HEIGHT, source=source)


def bars(sources: Sequence[str] = DEFAULT_BARS, height: int = 15) -> Widget:
    return Widget("bars", height, bars=tuple(sources))


def equalizer(height: int = 11) -> Widget:
    return Widget("equalizer", height)


def line() -> Widget:
    return Widget("separator", LINE_HEIGHT, style="line")


def space(height: int = 1) -> Widget:
    return Widget("separator", height, style="space")


def widget_from_json(raw: object) -> Widget:
    if not isinstance(raw, dict):
        raise TypeError("each widget must be a JSON object")
    kind = raw.get("kind")
    if not isinstance(kind, str):
        raise TypeError("each widget needs a 'kind'")
    allowed = {
        "weather": {"kind"},
        "temperature": {"kind", "source"},
        "bars": {"kind", "bars", "height"},
        "equalizer": {"kind", "height"},
        "separator": {"kind", "style", "height"},
    }.get(kind)
    if allowed is None:
        raise ValueError(f"unknown widget {kind!r} (known: {', '.join(WIDGET_KINDS)})")
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{kind} widget has unknown fields: {', '.join(sorted(unknown))}")
    if kind == "weather":
        return weather()
    if kind == "temperature":
        return temperature(_string(raw.get("source"), "temperature source"))
    if kind == "bars":
        sources = raw.get("bars")
        if not isinstance(sources, list):
            raise TypeError("bars must be a list of 3 source names")
        return Widget("bars", raw.get("height", 15), bars=tuple(sources))  # type: ignore[arg-type]
    if kind == "equalizer":
        return Widget("equalizer", raw.get("height", 11))  # type: ignore[arg-type]
    style = _string(raw.get("style"), "separator style")
    height = raw.get("height", LINE_HEIGHT if style == "line" else MIN_SPACE_HEIGHT)
    if style == "line" and height != LINE_HEIGHT:
        raise ValueError("a line separator is 1 row high")
    return Widget("separator", height, style=style)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Layout:
    """Widgets of the left and right panel. With "Swap panels" on, each shows the other's."""

    left: tuple[Widget, ...]
    right: tuple[Widget, ...]

    def __post_init__(self) -> None:
        for side in SIDES:
            widgets = getattr(self, side)
            if not isinstance(widgets, tuple) or not all(isinstance(w, Widget) for w in widgets):
                raise TypeError(f"the {side} panel must be a tuple of widgets")
            if len(widgets) > MAX_WIDGETS_PER_PANEL:
                raise ValueError(
                    f"the {side} panel has {len(widgets)} widgets, at most "
                    f"{MAX_WIDGETS_PER_PANEL} fit"
                )
            used = used_rows(widgets)
            if used > FRAME_HEIGHT:
                raise ValueError(
                    f"the {side} panel needs {used} rows, but it has {FRAME_HEIGHT}"
                )
            if sum(1 for widget in widgets if widget.kind == "equalizer") > 1:
                raise ValueError(f"the {side} panel can have one equalizer")

    def side(self, side: str) -> tuple[Widget, ...]:
        if side not in SIDES:
            raise ValueError(f"side must be 'left' or 'right', got {side!r}")
        return self.left if side == "left" else self.right

    def has(self, kind: str) -> bool:
        return any(widget.kind == kind for widget in (*self.left, *self.right))

    def to_json(self) -> dict[str, object]:
        return {side: [widget.to_json() for widget in self.side(side)] for side in SIDES}


def layout_from_json(raw: object) -> Layout:
    if not isinstance(raw, dict):
        raise TypeError("layout must be a JSON object with 'left' and 'right'")
    unknown = set(raw) - set(SIDES)
    if unknown:
        raise ValueError(f"layout has unknown fields: {', '.join(sorted(map(str, unknown)))}")
    panels = {}
    for side in SIDES:
        widgets = raw.get(side)
        if not isinstance(widgets, list):
            raise TypeError(f"layout.{side} must be a list of widgets")
        panels[side] = tuple(widget_from_json(widget) for widget in widgets)
    return Layout(**panels)


def used_rows(widgets: Sequence[Widget]) -> int:
    return sum(widget.height for widget in widgets)


def placed(widgets: Sequence[Widget]) -> Iterator[tuple[int, Widget]]:
    """(top row, widget) for each widget, stacked from the top."""
    top = 0
    for widget in widgets:
        yield top, widget
        top += widget.height


def default_layout(bar_sources: Sequence[str] = DEFAULT_BARS) -> Layout:
    """Outside temperature over the bars on the left; weather, GPU and CPU on the right."""
    return Layout(
        left=(
            temperature("outside"),
            space(),
            bars(bar_sources, 15),
            line(),
            space(),
            equalizer(11),
        ),
        right=(
            weather(),
            space(),
            temperature("gpu"),
            space(),
            temperature("cpu"),
            space(),
            equalizer(11),
        ),
    )


def classic_left(bar_sources: Sequence[str] = DEFAULT_BARS) -> tuple[Widget, ...]:
    """The left panel before layouts existed: tall bars on a line, the equalizer below."""
    return (bars(bar_sources, 21), line(), space(), equalizer(11))


def classic_right() -> tuple[Widget, ...]:
    """The right panel before layouts existed: weather, outside and CPU temperature."""
    return (
        weather(),
        space(),
        temperature("outside"),
        space(),
        temperature("cpu"),
        space(),
        equalizer(11),
    )


DEFAULT_LAYOUT = default_layout()


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value
