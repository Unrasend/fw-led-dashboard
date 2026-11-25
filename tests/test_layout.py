from __future__ import annotations

import pytest

from framework_led_dashboard.frame import FRAME_HEIGHT
from framework_led_dashboard.layout import (
    DEFAULT_LAYOUT,
    Layout,
    Widget,
    bars,
    classic_left,
    classic_right,
    default_layout,
    equalizer,
    layout_from_json,
    line,
    placed,
    space,
    temperature,
    used_rows,
    weather,
    widget_from_json,
)


def test_default_layout_puts_outside_temperature_over_the_bars_and_gpu_next_to_cpu() -> None:
    assert [w.label for w in DEFAULT_LAYOUT.left] == [
        "Outside temperature", "Space", "Bars", "Line", "Space", "Equalizer",
    ]
    assert [w.label for w in DEFAULT_LAYOUT.right] == [
        "Weather icon", "Space", "GPU temperature", "Space", "CPU temperature", "Space", "Equalizer",
    ]


@pytest.mark.parametrize(
    "widgets", [DEFAULT_LAYOUT.left, DEFAULT_LAYOUT.right, classic_left(), classic_right()]
)
def test_the_shipped_layouts_fill_the_panel_exactly(widgets: tuple[Widget, ...]) -> None:
    assert used_rows(widgets) == FRAME_HEIGHT


def test_classic_layout_puts_widgets_where_the_old_panels_drew_them() -> None:
    assert [(top, w.kind) for top, w in placed(classic_right())] == [
        (0, "weather"), (10, "separator"), (11, "temperature"), (16, "separator"),
        (17, "temperature"), (22, "separator"), (23, "equalizer"),
    ]
    assert [(top, w.height) for top, w in placed(classic_left())] == [
        (0, 21), (21, 1), (22, 1), (23, 11),
    ]


def test_default_layout_keeps_the_bar_sources_it_is_given() -> None:
    assert default_layout(("gpu", "swap", "disk")).left[2].bars == ("gpu", "swap", "disk")


@pytest.mark.parametrize(
    "widget",
    [weather(), temperature("cpu"), temperature("gpu"), temperature("outside"),
     bars(("disk", "gpu", "ram"), 7), equalizer(4), line(), space(3)],
)
def test_widgets_survive_a_json_round_trip(widget: Widget) -> None:
    assert widget_from_json(widget.to_json()) == widget


def test_layout_survives_a_json_round_trip() -> None:
    assert layout_from_json(DEFAULT_LAYOUT.to_json()) == DEFAULT_LAYOUT


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("weather", "JSON object"),
        ({"source": "cpu"}, "kind"),
        ({"kind": "clock"}, "clock"),
        ({"kind": "weather", "height": 3}, "unknown fields: height"),
        ({"kind": "temperature", "source": "fan"}, "source"),
        ({"kind": "temperature"}, "temperature source"),
        ({"kind": "bars", "bars": "cpu,ram,gpu"}, "bars"),
        ({"kind": "bars", "bars": ["cpu", "ram"]}, "bars"),
        ({"kind": "bars", "bars": ["cpu", "ram", "gpu"], "height": 2}, "3 to 34 rows"),
        ({"kind": "bars", "bars": ["cpu", "ram", "gpu"], "height": 35}, "3 to 34 rows"),
        ({"kind": "equalizer", "height": "11"}, "whole number"),
        ({"kind": "equalizer", "height": True}, "whole number"),
        ({"kind": "separator", "style": "dots"}, "style"),
        ({"kind": "separator", "style": "line", "height": 2}, "1 row"),
        ({"kind": "separator", "style": "space", "height": 0}, "1 to 34 rows"),
    ],
)
def test_widget_from_json_rejects_what_cannot_be_drawn(raw: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        widget_from_json(raw)


def test_layout_rejects_a_panel_that_needs_more_than_34_rows() -> None:
    with pytest.raises(ValueError, match="needs 35 rows"):
        Layout(left=(bars(height=24), equalizer(11)), right=())


def test_layout_allows_one_equalizer_per_panel() -> None:
    with pytest.raises(ValueError, match="one equalizer"):
        Layout(left=(equalizer(5), equalizer(5)), right=())
    assert Layout(left=(equalizer(5),), right=(equalizer(5),)).has("equalizer")


def test_layout_rejects_unknown_sides_and_non_widgets() -> None:
    with pytest.raises(ValueError, match="unknown fields: top"):
        layout_from_json({"left": [], "right": [], "top": []})
    with pytest.raises(TypeError, match="layout.right"):
        layout_from_json({"left": []})
    with pytest.raises(TypeError, match="tuple of widgets"):
        Layout(left=[weather()], right=())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="side"):
        DEFAULT_LAYOUT.side("middle")


def test_an_empty_panel_is_allowed() -> None:
    empty = Layout(left=(), right=())

    assert layout_from_json(empty.to_json()) == empty
    assert not empty.has("equalizer")
