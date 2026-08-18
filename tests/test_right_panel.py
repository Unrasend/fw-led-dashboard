from __future__ import annotations

from pathlib import Path

import pytest

from framework_led_dashboard.battery import BatteryMode, BatteryPower, BatteryPowerProvider
from framework_led_dashboard.frame import Frame
from framework_led_dashboard.pixel_font import draw_text
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.thermal import CpuTemperatureProvider


def test_pixel_font_draws_three_compact_glyphs_across_nine_columns() -> None:
    frame = Frame()

    draw_text(frame, "12W", x=0, y=18, intensity=200)

    rendered = tuple(
        "".join("#" if pixel else "." for pixel in row)
        for row in frame.snapshot()[18:23]
    )
    assert rendered == (
        ".#.####.#",
        "##...##.#",
        ".#.####.#",
        ".#.#..###",
        "#######.#",
    )


def test_pixel_font_rejects_unknown_glyphs() -> None:
    with pytest.raises(ValueError, match="unsupported glyph"):
        draw_text(Frame(), "?", x=0, y=0)


def _write_hwmon_sensor(
    root: Path,
    directory: str,
    *,
    name: str,
    label: str,
    value: str,
) -> None:
    sensor = root / directory
    sensor.mkdir(parents=True)
    (sensor / "name").write_text(f"{name}\n", encoding="utf-8")
    (sensor / "temp1_label").write_text(f"{label}\n", encoding="utf-8")
    (sensor / "temp1_input").write_text(f"{value}\n", encoding="utf-8")


def test_cpu_temperature_prefers_k10temp_tctl(tmp_path: Path) -> None:
    _write_hwmon_sensor(
        tmp_path,
        "hwmon0",
        name="cros_ec",
        label="cpu@4c",
        value="54000",
    )
    _write_hwmon_sensor(
        tmp_path,
        "hwmon1",
        name="k10temp",
        label="Tctl",
        value="66875",
    )

    assert CpuTemperatureProvider(hwmon_root=tmp_path).read_celsius() == pytest.approx(66.875)


def test_cpu_temperature_falls_back_to_cpu_labelled_sensor(tmp_path: Path) -> None:
    _write_hwmon_sensor(
        tmp_path,
        "hwmon0",
        name="cros_ec",
        label="cpu@4c",
        value="54000",
    )

    assert CpuTemperatureProvider(hwmon_root=tmp_path).read_celsius() == pytest.approx(54)


def test_cpu_temperature_returns_unknown_when_no_sensor_exists(tmp_path: Path) -> None:
    assert CpuTemperatureProvider(hwmon_root=tmp_path).read_celsius() is None


@pytest.mark.parametrize("value", ["not-a-number", "999999"])
def test_cpu_temperature_returns_unknown_for_bad_values(
    tmp_path: Path,
    value: str,
) -> None:
    _write_hwmon_sensor(
        tmp_path,
        "hwmon0",
        name="k10temp",
        label="Tctl",
        value=value,
    )

    assert CpuTemperatureProvider(hwmon_root=tmp_path).read_celsius() is None


def _write_battery_files(root: Path, **values: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (root / name).write_text(f"{value}\n", encoding="utf-8")


def test_battery_power_prefers_power_now_while_discharging(tmp_path: Path) -> None:
    _write_battery_files(
        tmp_path,
        status="Discharging",
        power_now="12360000",
        current_now="9000000",
        voltage_now="20000000",
    )

    reading = BatteryPowerProvider(battery_root=tmp_path).read()

    assert reading.mode is BatteryMode.DISCHARGING
    assert reading.watts == pytest.approx(12.36)


def test_battery_power_falls_back_to_current_times_voltage(tmp_path: Path) -> None:
    _write_battery_files(
        tmp_path,
        status="Discharging",
        current_now="800000",
        voltage_now="15000000",
    )

    reading = BatteryPowerProvider(battery_root=tmp_path).read()

    assert reading.mode is BatteryMode.DISCHARGING
    assert reading.watts == pytest.approx(12)


def test_battery_power_smooths_consecutive_discharge_samples(tmp_path: Path) -> None:
    _write_battery_files(tmp_path, status="Discharging", power_now="10000000")
    provider = BatteryPowerProvider(battery_root=tmp_path, smoothing_factor=0.5)
    assert provider.read().watts == pytest.approx(10)

    (tmp_path / "power_now").write_text("14000000\n", encoding="utf-8")

    assert provider.read().watts == pytest.approx(12)


@pytest.mark.parametrize(
    ("status", "expected_mode"),
    [("Charging", BatteryMode.CHARGING), ("Full", BatteryMode.AC), ("Not charging", BatteryMode.AC)],
)
def test_battery_power_reports_non_discharge_states(
    tmp_path: Path,
    status: str,
    expected_mode: BatteryMode,
) -> None:
    _write_battery_files(tmp_path, status=status, power_now="12000000")

    reading = BatteryPowerProvider(battery_root=tmp_path).read()

    assert reading.mode is expected_mode
    assert reading.watts is None


def test_battery_power_returns_unknown_for_missing_status(tmp_path: Path) -> None:
    reading = BatteryPowerProvider(battery_root=tmp_path).read()

    assert reading.mode is BatteryMode.UNKNOWN
    assert reading.watts is None


def test_battery_power_returns_unknown_watts_for_malformed_discharge_data(
    tmp_path: Path,
) -> None:
    _write_battery_files(
        tmp_path,
        status="Discharging",
        power_now="broken",
        current_now="broken",
        voltage_now="15000000",
    )

    reading = BatteryPowerProvider(battery_root=tmp_path).read()

    assert reading.mode is BatteryMode.DISCHARGING
    assert reading.watts is None


@pytest.mark.parametrize(
    ("temperature", "expected_text", "expected_x", "expected_spacing", "intensity"),
    [
        (54, "C54", 0, 0, 220),
        (105, "105", 0, 0, 220),
        (None, "--", 1, 1, 24),
        (float("nan"), "--", 1, 1, 24),
    ],
)
def test_right_panel_renders_cpu_temperature_in_rows_eighteen_to_twenty_two(
    temperature: float | None,
    expected_text: str,
    expected_x: int,
    expected_spacing: int,
    intensity: int,
) -> None:
    actual = render_right_panel(
        cpu_temperature_c=temperature,
        battery_power=BatteryPower(mode=BatteryMode.UNKNOWN),
    ).snapshot()
    expected = Frame()
    draw_text(
        expected,
        expected_text,
        x=expected_x,
        y=18,
        spacing=expected_spacing,
        intensity=intensity,
    )

    assert actual[18:23] == expected.snapshot()[18:23]


@pytest.mark.parametrize(
    ("power", "expected_text", "expected_x", "expected_spacing", "intensity"),
    [
        (BatteryPower(BatteryMode.DISCHARGING, 12.4), "12W", 0, 0, 220),
        (BatteryPower(BatteryMode.CHARGING), "CHG", 0, 0, 220),
        (BatteryPower(BatteryMode.AC), "AC", 1, 1, 220),
        (BatteryPower(BatteryMode.UNKNOWN), "--", 1, 1, 24),
        (BatteryPower(BatteryMode.DISCHARGING), "--", 1, 1, 24),
    ],
)
def test_right_panel_renders_power_state_in_rows_twenty_seven_to_thirty_one(
    power: BatteryPower,
    expected_text: str,
    expected_x: int,
    expected_spacing: int,
    intensity: int,
) -> None:
    actual = render_right_panel(cpu_temperature_c=None, battery_power=power).snapshot()
    expected = Frame()
    draw_text(
        expected,
        expected_text,
        x=expected_x,
        y=27,
        spacing=expected_spacing,
        intensity=intensity,
    )

    assert actual[27:32] == expected.snapshot()[27:32]


def test_right_panel_keeps_local_widgets_inside_assigned_rows() -> None:
    frame = render_right_panel(
        cpu_temperature_c=105,
        battery_power=BatteryPower(BatteryMode.DISCHARGING, 12),
    ).snapshot()

    lit_rows = {y for y, row in enumerate(frame) if any(row)}
    assert lit_rows <= set(range(9, 14)) | set(range(18, 23)) | set(range(27, 32))
