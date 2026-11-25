from __future__ import annotations

import pytest

from framework_led_dashboard.layout import (
    DEFAULT_LAYOUT,
    Layout,
    bars,
    equalizer,
    line,
    placed,
    space,
    temperature,
    weather,
)
from framework_led_dashboard.layout_edit import (
    ADDABLE,
    can_add,
    drop_index,
    free_at,
    free_rows,
    height_limit,
    insert_widget,
    move_widget,
    place_widget,
    remove_widget,
    resize_from_top,
    resize_widget,
    set_separator_style,
    widget_at,
)

ROOMY = Layout(
    left=(temperature("outside"), bars(height=10), equalizer(8)),  # 23 rows, 11 free
    right=(weather(), temperature("cpu")),  # 15 rows, 19 free
)


def labels(widgets) -> list[str]:
    return [widget.label for widget in widgets]


def test_widget_at_and_drop_index_follow_the_stack() -> None:
    widgets = ROOMY.left  # rows 0-4, 5-14, 15-22

    assert [widget_at(widgets, row) for row in (0, 4.9, 5, 14.5, 22, 23, 33)] == [
        0, 0, 1, 1, 2, None, None,
    ]
    # Above a widget's middle goes before it, below goes after it.
    assert [drop_index(widgets, row) for row in (0, 2.4, 2.6, 9.9, 10.1, 18.9, 19.1, 30)] == [
        0, 0, 1, 1, 2, 2, 3, 3,
    ]


def test_moving_down_counts_positions_before_the_move() -> None:
    moved, index = move_widget(ROOMY, "left", 0, "left", 3)  # outside temperature to the bottom

    assert labels(moved.left) == ["Bars", "Equalizer", "Outside temperature"]
    assert index == 2
    up, index = move_widget(ROOMY, "left", 2, "left", 0)
    assert (up.left[0].kind, index) == ("equalizer", 0)
    assert move_widget(ROOMY, "left", 1, "left", 2) == (ROOMY, 1)  # dropped where it was


def test_moving_to_the_other_panel_needs_room_and_no_second_equalizer() -> None:
    moved, index = move_widget(ROOMY, "left", 1, "right", 1)
    assert labels(moved.right) == ["Weather icon", "Bars", "CPU temperature"]
    assert index == 1
    assert labels(moved.left) == ["Outside temperature", "Equalizer"]

    full = DEFAULT_LAYOUT
    with pytest.raises(ValueError, match="No room on the right panel"):
        move_widget(full, "left", 0, "right", 0)
    both = Layout(left=(equalizer(5),), right=(equalizer(5),))
    with pytest.raises(ValueError, match="equalizer already"):
        move_widget(both, "left", 0, "right", 0)


def test_resizing_is_clamped_to_the_widget_and_the_free_rows() -> None:
    assert height_limit(ROOMY.left, 1) == (3, 21)  # 10 rows + 11 free
    assert resize_widget(ROOMY, "left", 1, 40).left[1].height == 21
    assert resize_widget(ROOMY, "left", 1, 1).left[1].height == 3
    assert resize_widget(ROOMY, "left", 1, 10) is ROOMY
    assert height_limit(ROOMY.left, 0) == (5, 5)  # a temperature is always 5 rows
    assert resize_widget(ROOMY, "left", 0, 9) is ROOMY


def test_full_panels_have_no_room_until_something_shrinks() -> None:
    assert free_rows(DEFAULT_LAYOUT.left) == 0
    assert "Needs 1 free row, this panel has 0" in can_add(DEFAULT_LAYOUT, "left", "space")
    shrunk = resize_widget(DEFAULT_LAYOUT, "left", 2, 10)  # the bars, 15 -> 10

    assert free_rows(shrunk.left) == 5
    assert can_add(shrunk, "left", "temperature") is None
    assert can_add(shrunk, "left", "weather") is not None  # 10 rows
    assert can_add(shrunk, "left", "equalizer") == "This panel has an equalizer already"


@pytest.mark.parametrize("kind", ADDABLE)
def test_every_widget_can_be_added_to_an_empty_panel(kind: str) -> None:
    layout, index = insert_widget(Layout(left=(), right=()), "right", 0, kind)

    assert len(layout.right) == 1 and index == 0
    assert layout.left == ()


def test_added_bars_and_equalizer_take_the_room_there_is_up_to_their_usual_height() -> None:
    assert insert_widget(ROOMY, "right", 2, "bars")[0].right[2].height == 15
    tight = Layout(left=(), right=(bars(height=30),))
    assert insert_widget(tight, "right", 0, "equalizer")[0].right[0].height == 4
    assert insert_widget(ROOMY, "left", 1, "line")[0].left[1] == line()


def test_insert_refuses_what_does_not_fit() -> None:
    with pytest.raises(ValueError, match="free"):
        insert_widget(DEFAULT_LAYOUT, "right", 0, "weather")


def test_remove_and_separator_style() -> None:
    layout = Layout(left=(space(3), weather()), right=())

    assert remove_widget(layout, "left", 0).left == (weather(),)
    assert set_separator_style(layout, "left", 0, "line") == (
        Layout(left=(line(), weather()), right=()), 0
    )
    lined = Layout(left=(line(),), right=())
    assert set_separator_style(lined, "left", 0, "space")[0].left == (space(1),)


def test_removing_a_widget_between_two_spaces_leaves_one_space() -> None:
    layout = Layout(left=(weather(), space(2), temperature("cpu"), space(3), equalizer(5)), right=())

    removed = remove_widget(layout, "left", 2)

    # The rows below move up as before; the two spaces around it become one.
    assert removed.left == (weather(), space(5), equalizer(5))


def test_moving_a_widget_out_from_between_spaces_joins_them() -> None:
    layout = Layout(left=(space(2), temperature("cpu"), space(3), weather()), right=())

    moved, index = move_widget(layout, "left", 1, "left", 4)
    assert (moved.left, index) == ((space(5), weather(), temperature("cpu")), 2)
    across, index = move_widget(layout, "left", 1, "right", 0)
    assert across.left == (space(5), weather())
    assert (across.right, index) == ((temperature("cpu"),), 0)


def test_a_new_or_changed_space_next_to_a_space_makes_it_taller() -> None:
    layout = Layout(left=(weather(), space(2), line(), temperature("cpu")), right=())

    added, index = insert_widget(layout, "left", 2, "space")
    assert (added.left, index) == ((weather(), space(3), line(), temperature("cpu")), 1)
    restyled, index = set_separator_style(layout, "left", 2, "space")
    assert (restyled.left, index) == ((weather(), space(3), temperature("cpu")), 1)
    below, index = insert_widget(layout, "left", 1, "space")  # above the space: joins it too
    assert (below.left, index) == ((weather(), space(3), line(), temperature("cpu")), 1)


def tops(widgets) -> list[tuple[int, str, int]]:
    return [(top, widget.label, widget.height) for top, widget in placed(widgets)]


SPACED = resize_widget(DEFAULT_LAYOUT, "left", 2, 10)  # bars 15 -> 10: rows 29-33 are free


def test_a_widget_slides_into_empty_rows_and_the_others_stay() -> None:
    assert free_at(SPACED, "left", 5, "left", 23)
    moved, index = place_widget(SPACED, "left", 5, "left", 23)  # the equalizer, from row 18

    assert index == 5
    assert tops(moved.left) == [
        (0, "Outside temperature", 5), (5, "Space", 1), (6, "Bars", 10), (16, "Line", 1),
        (17, "Space", 6), (23, "Equalizer", 11),
    ]


def test_a_widget_can_be_placed_over_spaces_and_splits_them() -> None:
    layout = Layout(left=(weather(), space(12), temperature("cpu")), right=())

    moved, index = place_widget(layout, "left", 2, "left", 14)

    assert tops(moved.left) == [
        (0, "Weather icon", 10), (10, "Space", 4), (14, "CPU temperature", 5),
    ]
    assert index == 2
    upward, index = place_widget(layout, "left", 2, "left", 10)
    assert tops(upward.left)[:2] == [(0, "Weather icon", 10), (10, "CPU temperature", 5)]
    assert index == 1


def test_placing_refuses_rows_that_hold_another_widget_or_lie_outside() -> None:
    assert not free_at(SPACED, "left", 0, "left", 3)  # onto the bars
    assert not free_at(SPACED, "left", 5, "left", 24)  # past the bottom
    assert not free_at(SPACED, "left", 5, "left", -1)
    with pytest.raises(ValueError, match="not free"):
        place_widget(SPACED, "left", 0, "left", 3)


def test_placing_on_the_other_panel_leaves_a_gap_where_it_was() -> None:
    layout = Layout(left=(temperature("cpu"), bars(height=10)), right=(weather(),))

    moved, index = place_widget(layout, "left", 0, "right", 20)

    assert tops(moved.left) == [(0, "Space", 5), (5, "Bars", 10)]
    assert tops(moved.right) == [(0, "Weather icon", 10), (10, "Space", 10), (20, "CPU temperature", 5)]
    assert index == 2
    both = Layout(left=(equalizer(5),), right=(equalizer(5),))
    assert not free_at(both, "left", 0, "right", 20)


def test_top_edge_takes_rows_from_a_space_above_and_removes_it_when_used_up() -> None:
    layout = Layout(left=(bars(height=10), space(2), equalizer(11)), right=())

    grown, index = resize_from_top(layout, "left", 2, 12)
    assert (tops(grown.left), index) == ([(0, "Bars", 10), (10, "Space", 1), (11, "Equalizer", 12)], 2)
    gone, index = resize_from_top(layout, "left", 2, 20)
    assert (tops(gone.left), index) == ([(0, "Bars", 10), (10, "Equalizer", 13)], 1)


def test_top_edge_borrows_from_resizable_neighbours_but_not_from_fixed_ones() -> None:
    layout = Layout(left=(temperature("cpu"), bars(height=10), equalizer(11)), right=())

    taller, _index = resize_from_top(layout, "left", 2, 15)
    assert tops(taller.left) == [(0, "CPU temperature", 5), (5, "Bars", 6), (11, "Equalizer", 15)]
    most, _index = resize_from_top(layout, "left", 2, 30)
    assert tops(most.left)[1] == (5, "Bars", 3)  # bars stop at their smallest
    assert resize_from_top(layout, "left", 1, 14) == (layout, 1)  # a temperature above
    assert resize_from_top(layout, "left", 0, 9) == (layout, 0)  # a fixed widget itself


def test_shrinking_from_the_top_gives_rows_above_or_leaves_a_space() -> None:
    layout = Layout(left=(temperature("cpu"), equalizer(11)), right=())

    smaller, index = resize_from_top(layout, "left", 1, 8)
    assert (tops(smaller.left), index) == (
        [(0, "CPU temperature", 5), (5, "Space", 3), (8, "Equalizer", 8)], 2
    )
    stacked = Layout(left=(bars(height=10), equalizer(11)), right=())
    shared, index = resize_from_top(stacked, "left", 1, 6)
    assert (tops(shared.left), index) == ([(0, "Bars", 15), (15, "Equalizer", 6)], 1)
    first, index = resize_from_top(Layout(left=(bars(height=10),), right=()), "left", 0, 7)
    assert (tops(first.left), index) == ([(0, "Space", 3), (3, "Bars", 7)], 1)


def test_only_a_separator_changes_style() -> None:
    with pytest.raises(ValueError, match="not a separator"):
        set_separator_style(Layout(left=(weather(),), right=()), "left", 0, "line")
