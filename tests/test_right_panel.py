from __future__ import annotations

from pathlib import Path

import pytest

from framework_led_dashboard.frame import Frame
from framework_led_dashboard.pixel_font import draw_text
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
