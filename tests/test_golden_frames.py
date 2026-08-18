from __future__ import annotations

from pathlib import Path

from framework_led_dashboard.battery import BatteryMode, BatteryPower
from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from framework_led_dashboard.left_panel import render_left_panel
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.system_metrics import SystemMetrics

GOLDEN_DIRECTORY = Path(__file__).parent / "goldens"
PIXEL_VALUES = {".": 0, "L": 128, "#": 220, "?": 24}
LEFT_LIFE_INPUT = (
    "101000101",
    "010101010",
    "001111100",
    "101000101",
    "011000110",
    "000101000",
    "000010000",
    "000000000",
    "001000100",
    "000101000",
    "000010000",
    "000000000",
)


def _load_golden(name: str) -> FramePixels:
    lines = (GOLDEN_DIRECTORY / name).read_text(encoding="utf-8").splitlines()
    assert len(lines) == FRAME_HEIGHT
    assert all(len(line) == FRAME_WIDTH for line in lines)
    assert all(pixel in PIXEL_VALUES for line in lines for pixel in line)
    return tuple(tuple(PIXEL_VALUES[pixel] for pixel in line) for line in lines)


def test_left_panel_matches_all_306_mockup_pixels() -> None:
    life = tuple(tuple(pixel == "1" for pixel in row) for row in LEFT_LIFE_INPUT)

    actual = render_left_panel(
        life,
        SystemMetrics(battery_percent=72, cpu_percent=38, ram_used_percent=61),
    ).snapshot()
    expected = _load_golden("left_dashboard.txt")

    assert sum(len(row) for row in expected) == 306
    assert actual == expected


def test_right_panel_local_widgets_match_all_306_mockup_pixels() -> None:
    actual = render_right_panel(
        cpu_temperature_c=54,
        battery_power=BatteryPower(BatteryMode.DISCHARGING, 12.4),
    ).snapshot()
    expected = _load_golden("right_local_metrics.txt")

    assert sum(len(row) for row in expected) == 306
    assert actual == expected
