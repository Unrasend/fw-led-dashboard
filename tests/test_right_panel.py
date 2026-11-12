from __future__ import annotations

from pathlib import Path

import pytest

from framework_led_dashboard.alerts import AlertState
from framework_led_dashboard.frame import Frame
from framework_led_dashboard.pixel_font import draw_text
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.thermal import CpuTemperatureProvider
from framework_led_dashboard.weather import WeatherReading


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


@pytest.mark.parametrize(
    ("temperature", "expected_text", "expected_x", "expected_spacing", "intensity"),
    [
        (54, "54", 1, 1, 220),
        (105, "105", 0, 0, 220),
        (None, "--", 1, 1, 24),
        (float("nan"), "--", 1, 1, 24),
    ],
)
def test_right_panel_renders_cpu_temperature_without_unit_in_rows_seventeen_to_twenty_one(
    temperature: float | None,
    expected_text: str,
    expected_x: int,
    expected_spacing: int,
    intensity: int,
) -> None:
    actual = render_right_panel(cpu_temperature_c=temperature).snapshot()
    expected = Frame()
    draw_text(expected, expected_text, x=expected_x, y=17, spacing=expected_spacing, intensity=intensity)

    assert actual[17:22] == expected.snapshot()[17:22]


def test_right_panel_keeps_local_widgets_inside_assigned_rows() -> None:
    frame = render_right_panel(cpu_temperature_c=105).snapshot()

    lit_rows = {y for y, row in enumerate(frame) if any(row)}
    assert lit_rows <= set(range(11, 16)) | set(range(17, 22))


def test_right_panel_draws_equalizer_in_rows_twenty_three_to_thirty_three() -> None:
    levels = (1.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1 / 11)

    frame = render_right_panel(cpu_temperature_c=None, spectrum=levels).snapshot()

    column = lambda x: tuple(frame[y][x] for y in range(23, 34))
    assert column(0) == (220,) + (128,) * 10
    assert column(1) == (0,) * 5 + (220,) + (128,) * 5
    assert column(2) == (0,) * 11
    assert column(8) == (0,) * 10 + (220,)
    assert all(frame[y][x] == 0 for y in range(22, 23) for x in range(9))


def test_right_panel_leaves_equalizer_dark_without_microphone() -> None:
    frame = render_right_panel(cpu_temperature_c=54, spectrum=None).snapshot()

    assert all(pixel == 0 for y in range(22, 34) for pixel in frame[y])


@pytest.mark.parametrize(
    ("alerts", "hidden_rows"),
    [
        (AlertState(outside_temperature=True), range(11, 16)),
        (AlertState(cpu_temperature=True), range(17, 22)),
    ],
)
def test_right_panel_blinks_only_the_critical_value(
    alerts: AlertState,
    hidden_rows: range,
) -> None:
    arguments = {
        "weather": WeatherReading(18.4, 2, True),
        "cpu_temperature_c": 120,
        "spectrum": (0.3,) * 9,
        "alerts": alerts,
    }
    visible = render_right_panel(**arguments, blink_on=True).snapshot()
    hidden = render_right_panel(**arguments, blink_on=False).snapshot()

    assert all(pixel == 0 for y in hidden_rows for pixel in hidden[y])
    other_rows = set(range(34)) - set(hidden_rows)
    assert all(hidden[y] == visible[y] for y in other_rows)


@pytest.mark.parametrize(
    ("temperature", "expected_rows"),
    [
        pytest.param(
            29.0,
            (
                "######..#",
                "..##.#...",
                "######...",
                "#....#...",
                "######...",
            ),
            id="29-unchanged",
        ),
        pytest.param(
            2.0,
            (
                "..###...#",
                "....#....",
                "..###....",
                "..#......",
                "..###....",
            ),
            id="2-unchanged",
        ),
        pytest.param(
            -1.0,
            (
                ".....#..#",
                "....##...",
                "###..#...",
                ".....#...",
                "....###..",
            ),
            id="minus-1",
        ),
        pytest.param(
            -5.0,
            (
                "....###.#",
                "....#....",
                "###.###..",
                "......#..",
                "....###..",
            ),
            id="minus-5",
        ),
        pytest.param(
            -9.6,
            (
                "...#..###",
                "..##..#.#",
                "#..#..#.#",
                "...#..#.#",
                "..###.###",
            ),
            id="minus-10",
        ),
        pytest.param(
            -15.0,
            (
                "...#..###",
                "..##..#..",
                "#..#..###",
                "...#....#",
                "..###.###",
            ),
            id="minus-15",
        ),
        pytest.param(
            -20.0,
            (
                "..###.###",
                "....#.#.#",
                "#.###.#.#",
                "..#...#.#",
                "..###.###",
            ),
            id="minus-20",
        ),
        pytest.param(
            -99.6,
            (
                "..###.###",
                "..#.#.#.#",
                "#.###.###",
                "....#...#",
                "..###.###",
            ),
            id="clamp-99",
        ),
    ],
)
def test_right_panel_keeps_negative_outside_temperature_glyphs_apart(
    temperature: float,
    expected_rows: tuple[str, ...],
) -> None:
    frame = render_right_panel(
        cpu_temperature_c=None,
        weather=WeatherReading(temperature, 0, True),
    ).snapshot()

    rendered = tuple("".join("#" if pixel else "." for pixel in row) for row in frame[11:16])
    assert rendered == expected_rows


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_cpu_temperature_keeps_reading_the_sensor_it_found(tmp_path: Path) -> None:
    _write_hwmon_sensor(tmp_path, "hwmon0", name="cros_ec", label="cpu@4c", value="54000")
    clock = _ManualClock()
    provider = CpuTemperatureProvider(hwmon_root=tmp_path, clock=clock, rescan_seconds=60)
    assert provider.read_celsius() == pytest.approx(54)

    # A better sensor appears, the chosen one changes value: no search within a minute...
    _write_hwmon_sensor(tmp_path, "hwmon1", name="k10temp", label="Tctl", value="70000")
    (tmp_path / "hwmon0" / "temp1_input").write_text("55000", encoding="utf-8")
    clock.now = 59.0
    assert provider.read_celsius() == pytest.approx(55)

    # ...and the search after it picks the better one.
    clock.now = 60.0
    assert provider.read_celsius() == pytest.approx(70)


def test_cpu_temperature_searches_again_when_its_sensor_disappears(tmp_path: Path) -> None:
    _write_hwmon_sensor(tmp_path, "hwmon0", name="k10temp", label="Tctl", value="66000")
    clock = _ManualClock()
    provider = CpuTemperatureProvider(hwmon_root=tmp_path, clock=clock, rescan_seconds=60)
    assert provider.read_celsius() == pytest.approx(66)

    # hwmon numbers can change when a driver reloads.
    (tmp_path / "hwmon0").rename(tmp_path / "hwmon3")
    clock.now = 1.0

    assert provider.read_celsius() == pytest.approx(66)


def test_cpu_temperature_without_sensors_waits_before_searching_again(tmp_path: Path) -> None:
    clock = _ManualClock()
    provider = CpuTemperatureProvider(hwmon_root=tmp_path, clock=clock, rescan_seconds=60)
    assert provider.read_celsius() is None

    _write_hwmon_sensor(tmp_path, "hwmon0", name="k10temp", label="Tctl", value="50000")
    clock.now = 30.0
    assert provider.read_celsius() is None
    clock.now = 60.0
    assert provider.read_celsius() == pytest.approx(50)
