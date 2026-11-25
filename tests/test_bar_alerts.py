from __future__ import annotations

import pytest

from framework_led_dashboard.bar_alerts import BarBlinker, describe_rule
from framework_led_dashboard.layout import (
    DEFAULT_LAYOUT,
    BlinkRule,
    Layout,
    bar_rules,
    bars,
    blink_rule_from_json,
    default_blink,
    widget_from_json,
)
from framework_led_dashboard.system_metrics import SystemMetrics

CPU_HIGH = ("cpu", BlinkRule("above", 95))
BATTERY_LOW = ("battery", BlinkRule("below", 10))


def metrics(*, cpu: float | None = 30, battery: float | None = 80, ram: float | None = 40) -> SystemMetrics:
    return SystemMetrics(battery_percent=battery, cpu_percent=cpu, ram_used_percent=ram)


def test_defaults_are_cpu_and_ram_at_95_and_battery_at_10() -> None:
    assert default_blink("cpu") == BlinkRule("above", 95)
    assert default_blink("ram") == BlinkRule("above", 95)
    assert default_blink("battery") == BlinkRule("below", 10)
    assert {default_blink(key).when for key in ("gpu", "swap", "disk")} == {"never"}
    assert bar_rules(DEFAULT_LAYOUT) == {
        ("battery", BlinkRule("below", 10)),
        ("cpu", BlinkRule("above", 95)),
        ("ram", BlinkRule("above", 95)),
    }


def test_above_blinks_at_the_threshold_and_stops_two_points_below() -> None:
    blinker = BarBlinker()

    states = [CPU_HIGH in blinker.update({CPU_HIGH}, metrics(cpu=value)) for value in (94.9, 95, 93.5, 93, 92.9)]

    assert states == [False, True, True, True, False]


def test_below_blinks_at_the_threshold_and_stops_two_points_above() -> None:
    blinker = BarBlinker()

    states = [BATTERY_LOW in blinker.update({BATTERY_LOW}, metrics(battery=value)) for value in (11, 10, 12, 12.1)]

    assert states == [False, True, True, False]


def test_never_unknown_values_and_removed_rules_do_not_blink() -> None:
    blinker = BarBlinker()

    assert blinker.update({("gpu", BlinkRule())}, metrics()) == frozenset()
    assert blinker.update({CPU_HIGH}, metrics(cpu=None)) == frozenset()
    assert blinker.update({CPU_HIGH}, metrics(cpu=float("nan"))) == frozenset()
    assert blinker.update({CPU_HIGH}, metrics(cpu=99)) == {CPU_HIGH}
    assert blinker.update(set(), metrics(cpu=99)) == frozenset()  # rule gone from the layout


def test_two_bars_of_one_source_blink_by_their_own_rules() -> None:
    lower = ("cpu", BlinkRule("above", 50))

    assert BarBlinker().update({CPU_HIGH, lower}, metrics(cpu=70)) == {lower}


def test_rules_survive_json_and_old_bars_get_their_sources_defaults() -> None:
    widget = bars(("gpu", "cpu", "battery"), 9, (BlinkRule("above", 80), BlinkRule(), BlinkRule("below", 25)))

    assert widget_from_json(widget.to_json()) == widget
    old = widget_from_json({"kind": "bars", "bars": ["swap", "ram", "battery"], "height": 12})
    assert old.blink == (BlinkRule(), BlinkRule("above", 95), BlinkRule("below", 10))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"when": "sometimes", "percent": 5}, "when"),
        ({"when": "above", "percent": 101}, "between 0 and 100"),
        ({"when": "above", "percent": 9.5}, "whole number"),
        ({"when": "above", "percent": 5, "color": 1}, "unknown fields"),
        ("above 95", "JSON object"),
    ],
)
def test_bad_rules_are_refused(raw: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        blink_rule_from_json(raw)


def test_bars_need_one_rule_per_bar() -> None:
    with pytest.raises(ValueError, match="one blink rule per bar"):
        widget_from_json({"kind": "bars", "bars": ["cpu", "ram", "gpu"], "blink": [{"when": "never"}]})
    with pytest.raises(ValueError, match="one blink rule per bar"):
        Layout(left=(bars(blink=()),), right=())


def test_rules_read_well_in_the_log() -> None:
    assert describe_rule("battery", BlinkRule("below", 10)) == "Battery ≤ 10 %"
    assert describe_rule("cpu", BlinkRule("above", 95)) == "CPU ≥ 95 %"
