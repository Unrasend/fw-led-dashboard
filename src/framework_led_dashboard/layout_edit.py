"""Edits the settings window makes to a layout, kept free of GTK so they can be tested.

Every function returns a new Layout (with the edited widget's new index where it can move)
or raises ValueError with a message fit for the window (no room, a second equalizer on one
panel, ...). Spaces that end up next to each other become one space.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .frame import FRAME_HEIGHT
from .layout import (
    SIDES,
    Layout,
    Widget,
    bars,
    equalizer,
    line,
    placed,
    space,
    temperature,
    used_rows,
    weather,
)

ADDABLE = ("weather", "temperature", "bars", "equalizer", "line", "space")
ADD_LABELS = {
    "weather": "Weather icon",
    "temperature": "Temperature",
    "bars": "Bars",
    "equalizer": "Equalizer",
    "line": "Line",
    "space": "Space",
}
PREFERRED_HEIGHTS = {"bars": 15, "equalizer": 11}


def free_rows(widgets: Sequence[Widget]) -> int:
    return FRAME_HEIGHT - used_rows(widgets)


def other_side(side: str) -> str:
    return "right" if side == "left" else "left"


def with_side(layout: Layout, side: str, widgets: Sequence[Widget]) -> Layout:
    """`layout` with the widgets of `side` replaced; Layout checks rows and equalizers."""
    if side not in SIDES:
        raise ValueError(f"side must be 'left' or 'right', got {side!r}")
    return replace(layout, **{side: tuple(widgets)})


def widget_at(widgets: Sequence[Widget], row: float) -> int | None:
    """Index of the widget covering `row` (fractions allowed), or None below the last one."""
    for index, (top, widget) in enumerate(placed(widgets)):
        if top <= row < top + widget.height:
            return index
    return None


def drop_index(widgets: Sequence[Widget], row: float) -> int:
    """Where a widget dropped at `row` goes: before the first widget whose middle is below."""
    for index, (top, widget) in enumerate(placed(widgets)):
        if row < top + widget.height / 2:
            return index
    return len(widgets)


def new_widget(kind: str, room: int) -> Widget:
    """A fresh widget of an ADDABLE kind; bars and the equalizer take what room there is, up
    to their usual height."""
    if kind == "weather":
        return weather()
    if kind == "temperature":
        return temperature("cpu")
    if kind == "line":
        return line()
    if kind == "space":
        return space(1)
    if kind not in PREFERRED_HEIGHTS:
        raise ValueError(f"unknown widget {kind!r}")
    height = max(1, min(PREFERRED_HEIGHTS[kind], room))
    return bars(height=height) if kind == "bars" else equalizer(height)


def minimum_height(kind: str) -> int:
    """Rows a new widget of an ADDABLE kind needs at least."""
    sample = {"bars": bars(height=3), "equalizer": equalizer(3)}.get(kind) or new_widget(kind, 0)
    return sample.height_range[0]


def can_add(layout: Layout, side: str, kind: str) -> str | None:
    """None if a `kind` widget fits on `side`, else why not."""
    widgets = layout.side(side)
    room = free_rows(widgets)
    if kind == "equalizer" and any(widget.kind == "equalizer" for widget in widgets):
        return "This panel has an equalizer already"
    needed = minimum_height(kind)
    if room < needed:
        return (
            f"Needs {needed} free {'row' if needed == 1 else 'rows'}, this panel has {room}. "
            "Remove a widget or make bars, the equalizer or a space smaller."
        )
    return None


def merge_spaces(widgets: Sequence[Widget], keep: int = -1) -> tuple[list[Widget], int]:
    """Neighbouring spaces as one space. Returns the widgets and where widget `keep` is now
    (a space merged into the one above it is that one)."""
    merged: list[Widget] = []
    kept = -1
    for index, widget in enumerate(widgets):
        if merged and _is_space(widget) and _is_space(merged[-1]):
            merged[-1] = space(merged[-1].height + widget.height)
        else:
            merged.append(widget)
        if index == keep:
            kept = len(merged) - 1
    return merged, kept


def _with_merged(layout: Layout, side: str, widgets: Sequence[Widget], keep: int) -> tuple[Layout, int]:
    merged, kept = merge_spaces(widgets, keep)
    return with_side(layout, side, merged), kept


def insert_widget(layout: Layout, side: str, index: int, kind: str) -> tuple[Layout, int]:
    """Add a new widget at `index`; a space next to a space makes that one taller."""
    reason = can_add(layout, side, kind)
    if reason is not None:
        raise ValueError(reason)
    widgets = list(layout.side(side))
    index = max(0, min(index, len(widgets)))
    widgets.insert(index, new_widget(kind, free_rows(widgets)))
    return _with_merged(layout, side, widgets, index)


def remove_widget(layout: Layout, side: str, index: int) -> Layout:
    widgets = list(layout.side(side))
    del widgets[index]
    return _with_merged(layout, side, widgets, -1)[0]


def replace_widget(layout: Layout, side: str, index: int, widget: Widget) -> Layout:
    widgets = list(layout.side(side))
    widgets[index] = widget
    return with_side(layout, side, widgets)


def move_widget(
    layout: Layout, side: str, index: int, to_side: str, to_index: int
) -> tuple[Layout, int]:
    """Move a widget; `to_index` counts positions before the move (as drop_index gives them).
    Returns the layout and the widget's new index on `to_side`."""
    widgets = list(layout.side(side))
    widget = widgets.pop(index)
    if to_side == side:
        if to_index > index:
            to_index -= 1
        to_index = max(0, min(to_index, len(widgets)))
        widgets.insert(to_index, widget)
        return _with_merged(layout, side, widgets, to_index)
    target = list(layout.side(to_side))
    if widget.kind == "equalizer" and any(other.kind == "equalizer" for other in target):
        raise ValueError(f"The {to_side} panel has an equalizer already")
    if free_rows(target) < widget.height:
        raise ValueError(
            f"No room on the {to_side} panel: it has {free_rows(target)} free rows, "
            f"this needs {widget.height}"
        )
    to_index = max(0, min(to_index, len(target)))
    target.insert(to_index, widget)
    source, _kept = merge_spaces(widgets)
    return _with_merged(with_side(layout, side, source), to_side, target, to_index)


def height_limit(widgets: Sequence[Widget], index: int) -> tuple[int, int]:
    """Smallest and largest height widget `index` can take where it stands."""
    widget = widgets[index]
    low, high = widget.height_range
    return low, min(high, widget.height + free_rows(widgets))


def resize_widget(layout: Layout, side: str, index: int, height: int) -> Layout:
    """Set a height, clamped to what the widget allows and the panel has room for."""
    widgets = layout.side(side)
    low, high = height_limit(widgets, index)
    clamped = max(low, min(high, height))
    if clamped == widgets[index].height:
        return layout
    return replace_widget(layout, side, index, replace(widgets[index], height=clamped))


def _is_space(widget: Widget) -> bool:
    return widget.kind == "separator" and widget.style == "space"


def resize_from_top(layout: Layout, side: str, index: int, height: int) -> tuple[Layout, int]:
    """Set a height by moving the widget's top edge: only the border with the widget above
    moves, nothing else on the panel does. Returns the new layout and the widget's new index
    (a space above it can appear or disappear).

    Growing takes the rows from the widget above: a space gives all of them (and goes), bars
    or an equalizer down to their smallest; a fixed-height widget gives none. Shrinking gives
    the rows to a space, bars or an equalizer above, or else puts a new space above it.
    """
    widgets = list(layout.side(side))
    widget = widgets[index]
    low, high = widget.height_range
    change = max(low, min(high, height)) - widget.height
    above = widgets[index - 1] if index > 0 else None
    if change > 0:
        if above is None:
            return layout, index
        if _is_space(above):
            given = min(change, above.height)
        elif above.resizable:
            given = min(change, above.height - above.height_range[0])
        else:
            given = 0
        if given == 0:
            return layout, index
        widgets[index] = replace(widget, height=widget.height + given)
        if _is_space(above) and given == above.height:
            del widgets[index - 1]
            index -= 1
        else:
            widgets[index - 1] = replace(above, height=above.height - given)
    elif change < 0:
        freed = -change
        widgets[index] = replace(widget, height=widget.height - freed)
        if above is not None and (_is_space(above) or above.resizable):
            widgets[index - 1] = replace(above, height=above.height + freed)
        else:
            widgets.insert(index, space(freed))
            index += 1
    else:
        return layout, index
    return with_side(layout, side, widgets), index


def _stacked(items: Sequence[tuple[int, Widget]]) -> list[Widget]:
    """Widgets at fixed rows back into a stack: the gaps between them become spaces."""
    stack: list[Widget] = []
    row = 0
    for top, widget in sorted(items, key=lambda item: item[0]):
        if top > row:
            stack.append(space(top - row))
        stack.append(widget)
        row = top + widget.height
    return stack


def _fixed(widgets: Sequence[Widget], *, without: int | None = None) -> list[tuple[int, Widget]]:
    """(top, widget) of everything but spaces (they are just gaps here) and `without`."""
    return [
        (top, widget)
        for index, (top, widget) in enumerate(placed(widgets))
        if index != without and not _is_space(widget)
    ]


def free_at(
    layout: Layout, side: str, index: int, to_side: str, top: int
) -> bool:
    """True if widget `index` of `side` fits at row `top` of `to_side` over empty rows only."""
    widget = layout.side(side)[index]
    if widget.kind == "equalizer" and to_side != side and any(
        other.kind == "equalizer" for other in layout.side(to_side)
    ):
        return False
    if top < 0 or top + widget.height > FRAME_HEIGHT:
        return False
    others = _fixed(layout.side(to_side), without=index if to_side == side else None)
    bottom = top + widget.height
    return all(bottom <= start or start + other.height <= top for start, other in others)


def place_widget(
    layout: Layout, side: str, index: int, to_side: str, top: int
) -> tuple[Layout, int]:
    """Put a widget at row `top` of `to_side`, over empty rows only; every other widget keeps
    its row, and the gaps around them become spaces. Returns the layout and the new index."""
    if not free_at(layout, side, index, to_side, top):
        raise ValueError("That place is not free")
    widget = layout.side(side)[index]
    source = _fixed(layout.side(side), without=index)
    if to_side == side:
        stack = _stacked([*source, (top, widget)])
        return with_side(layout, side, stack), stack.index(widget, _count_before(stack, top))
    target = _stacked([*_fixed(layout.side(to_side)), (top, widget)])
    moved = with_side(with_side(layout, side, _stacked(source)), to_side, target)
    return moved, target.index(widget, _count_before(target, top))


def _count_before(stack: Sequence[Widget], top: int) -> int:
    """How many widgets of `stack` start above row `top` (to find the placed one)."""
    return sum(1 for start, _widget in placed(stack) if start < top)


def set_separator_style(
    layout: Layout, side: str, index: int, style: str
) -> tuple[Layout, int]:
    """Turn a separator into a line or a space; a space next to a space joins it."""
    widgets = list(layout.side(side))
    if widgets[index].kind != "separator":
        raise ValueError(f"{widgets[index].label} is not a separator")
    widgets[index] = line() if style == "line" else space(widgets[index].height)
    return _with_merged(layout, side, widgets, index)
