from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH, Frame
from framework_led_dashboard.left_panel import render_left_panel
from framework_led_dashboard.system_metrics import SystemMetrics, SystemMetricsProvider


def test_frame_is_exactly_nine_by_thirty_four_and_starts_dark() -> None:
    frame = Frame()

    pixels = frame.snapshot()
    assert FRAME_WIDTH == 9
    assert FRAME_HEIGHT == 34
    assert len(pixels) == 34
    assert all(len(row) == 9 for row in pixels)
    assert all(pixel == 0 for row in pixels for pixel in row)


def test_frame_clamps_pixel_values_to_grayscale_byte_range() -> None:
    frame = Frame()

    frame.set_pixel(0, 0, -10)
    frame.set_pixel(1, 0, 128)
    frame.set_pixel(2, 0, 999)

    assert frame.snapshot()[0][:3] == (0, 128, 255)


def test_frame_fills_rectangles_without_touching_surrounding_pixels() -> None:
    frame = Frame()

    frame.fill_rect(x=2, y=3, width=2, height=3, value=80)

    lit_pixels = {
        (x, y)
        for y, row in enumerate(frame.snapshot())
        for x, value in enumerate(row)
        if value > 0
    }
    assert lit_pixels == {(2, 3), (3, 3), (2, 4), (3, 4), (2, 5), (3, 5)}


@pytest.mark.parametrize(
    ("x", "y"),
    [(-1, 0), (0, -1), (FRAME_WIDTH, 0), (0, FRAME_HEIGHT)],
)
def test_frame_rejects_out_of_bounds_pixels(x: int, y: int) -> None:
    with pytest.raises(IndexError, match="outside"):
        Frame().set_pixel(x, y, 1)


def test_system_metrics_reads_battery_and_ram_while_cpu_waits_for_second_sample(
    tmp_path: Path,
) -> None:
    proc_stat = tmp_path / "stat"
    meminfo = tmp_path / "meminfo"
    capacity = tmp_path / "capacity"
    proc_stat.write_text("cpu  100 0 50 850 0 0 0 0\n", encoding="utf-8")
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n", encoding="utf-8")
    capacity.write_text("48\n", encoding="utf-8")
    provider = SystemMetricsProvider(
        proc_stat_path=proc_stat,
        meminfo_path=meminfo,
        battery_capacity_path=capacity,
    )

    metrics = provider.read()

    assert metrics.battery_percent == 48
    assert metrics.ram_used_percent == 75
    assert metrics.cpu_percent is None


def test_system_metrics_calculates_cpu_usage_from_consecutive_samples(tmp_path: Path) -> None:
    proc_stat = tmp_path / "stat"
    meminfo = tmp_path / "meminfo"
    capacity = tmp_path / "capacity"
    proc_stat.write_text("cpu  100 0 50 850 0 0 0 0\n", encoding="utf-8")
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n", encoding="utf-8")
    capacity.write_text("48\n", encoding="utf-8")
    provider = SystemMetricsProvider(
        proc_stat_path=proc_stat,
        meminfo_path=meminfo,
        battery_capacity_path=capacity,
    )
    provider.read()

    proc_stat.write_text("cpu  150 0 70 880 0 0 0 0\n", encoding="utf-8")
    metrics = provider.read()

    assert metrics.cpu_percent == pytest.approx(70)


def test_system_metrics_returns_unknown_for_missing_or_malformed_sources(tmp_path: Path) -> None:
    provider = SystemMetricsProvider(
        proc_stat_path=tmp_path / "missing-stat",
        meminfo_path=tmp_path / "missing-meminfo",
        battery_capacity_path=tmp_path / "missing-capacity",
    )

    metrics = provider.read()

    assert metrics.battery_percent is None
    assert metrics.cpu_percent is None
    assert metrics.ram_used_percent is None


def _gpu_card(root: Path, name: str, busy: str, runtime_status: str = "active") -> None:
    device = root / name / "device"
    (device / "power").mkdir(parents=True)
    (device / "gpu_busy_percent").write_text(busy, encoding="utf-8")
    (device / "power" / "runtime_status").write_text(f"{runtime_status}\n", encoding="utf-8")


def _statvfs(blocks: int, free: int, available: int):
    return lambda _path: SimpleNamespace(f_blocks=blocks, f_bfree=free, f_bavail=available)


def test_system_metrics_reads_gpu_swap_and_disk(tmp_path: Path) -> None:
    drm = tmp_path / "drm"
    _gpu_card(drm, "card1", "16\n")
    _gpu_card(drm, "card2", "40\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 400 kB\nSwapFree: 100 kB\n",
        encoding="utf-8",
    )
    provider = SystemMetricsProvider(
        proc_stat_path=tmp_path / "missing-stat",
        meminfo_path=meminfo,
        battery_capacity_path=tmp_path / "missing-capacity",
        drm_root=drm,
        disk_path=tmp_path,
        statvfs=_statvfs(blocks=1000, free=300, available=200),
    )

    metrics = provider.read()

    assert metrics.gpu_percent == 40
    assert metrics.swap_used_percent == 75
    # df semantics: used / (used + available to unprivileged users) = 700 / 900.
    assert metrics.disk_used_percent == pytest.approx(77.78, abs=0.01)


def test_system_metrics_does_not_wake_a_runtime_suspended_gpu(tmp_path: Path) -> None:
    drm = tmp_path / "drm"
    _gpu_card(drm, "card0", "99\n", runtime_status="suspended")
    _gpu_card(drm, "card1", "12\n")
    provider = SystemMetricsProvider(
        proc_stat_path=tmp_path / "missing-stat",
        meminfo_path=tmp_path / "missing-meminfo",
        battery_capacity_path=tmp_path / "missing-capacity",
        drm_root=drm,
        statvfs=_statvfs(blocks=0, free=0, available=0),
    )

    assert provider.read().gpu_percent == 12


def test_system_metrics_reports_unknown_gpu_swap_and_disk(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
        encoding="utf-8",
    )

    def failing_statvfs(_path: Path) -> SimpleNamespace:
        raise OSError("gone")

    provider = SystemMetricsProvider(
        proc_stat_path=tmp_path / "missing-stat",
        meminfo_path=meminfo,
        battery_capacity_path=tmp_path / "missing-capacity",
        drm_root=tmp_path / "no-drm",
        statvfs=failing_statvfs,
    )

    metrics = provider.read()

    assert metrics.gpu_percent is None
    assert metrics.swap_used_percent is None
    assert metrics.disk_used_percent is None
    assert metrics.ram_used_percent == 75


def test_left_panel_draws_the_sources_chosen_for_each_bar() -> None:
    metrics = SystemMetrics(
        battery_percent=0,
        cpu_percent=0,
        ram_used_percent=0,
        gpu_percent=100,
        swap_used_percent=50,
        disk_used_percent=None,
    )

    chosen = render_left_panel(metrics, bars=("gpu", "swap", "disk")).snapshot()
    expected = render_left_panel(
        SystemMetrics(battery_percent=100, cpu_percent=50, ram_used_percent=None)
    ).snapshot()

    assert chosen == expected


def test_left_panel_blinks_the_bar_that_shows_the_critical_source() -> None:
    metrics = SystemMetrics(
        battery_percent=100, cpu_percent=100, ram_used_percent=100, gpu_percent=100
    )
    blinking = {"battery"}
    bars = ("gpu", "cpu", "battery")

    hidden = render_left_panel(metrics, bars=bars, blinking=blinking, blink_on=False)
    shown = render_left_panel(metrics, bars=bars, blinking=blinking, blink_on=True)

    dark_columns = {
        x for x in range(FRAME_WIDTH) if all(hidden.snapshot()[y][x] == 0 for y in range(21))
    }
    assert {7, 8} <= dark_columns
    assert {0, 1, 3, 4, 5}.isdisjoint(dark_columns)
    assert shown.snapshot() != hidden.snapshot()


@pytest.mark.parametrize(
    "bars", [("cpu", "ram"), ("cpu", "ram", "fan"), ("cpu", "ram", "gpu", "disk")]
)
def test_left_panel_rejects_invalid_bar_choices(bars: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="bars"):
        render_left_panel(SystemMetrics(1, 1, 1), bars=bars)


@pytest.mark.parametrize(
    ("percent", "first_lit_row"),
    [(0, None), (50, 10), (100, 0)],
)
def test_left_panel_bar_snapshots_fill_from_bottom(
    percent: float,
    first_lit_row: int | None,
) -> None:
    frame = render_left_panel(
        SystemMetrics(battery_percent=percent, cpu_percent=percent, ram_used_percent=percent),
    ).snapshot()

    expected_rows = set() if first_lit_row is None else set(range(first_lit_row, 21))
    for columns in ((0, 1), (3, 4, 5), (7, 8)):
        lit_rows = {y for y in range(21) if any(frame[y][column] > 0 for column in columns)}
        assert lit_rows == expected_rows


def test_left_panel_joins_bars_with_a_full_width_base_row() -> None:
    frame = render_left_panel(
        SystemMetrics(battery_percent=0, cpu_percent=0, ram_used_percent=0),
    ).snapshot()

    assert frame[21] == (220,) * 9
    assert frame[22] == (0,) * 9
    assert all(frame[y] == (0,) * 9 for y in range(21))


def test_left_panel_keeps_bars_inside_assigned_columns() -> None:
    frame = render_left_panel(
        SystemMetrics(battery_percent=100, cpu_percent=100, ram_used_percent=100),
    ).snapshot()

    assert all(frame[y][column] == 0 for y in range(21) for column in (2, 6))
    assert all(frame[y][column] == 220 for y in range(21) for column in (0, 1, 3, 4, 5, 7, 8))


def test_left_panel_marks_missing_bars_with_dim_bottom_pixels() -> None:
    frame = render_left_panel(
        SystemMetrics(battery_percent=None, cpu_percent=None, ram_used_percent=None),
    ).snapshot()

    assert all(frame[20][column] == 24 for column in (0, 1, 3, 4, 5, 7, 8))
    assert all(frame[y][column] == 0 for y in range(20) for column in (0, 1, 3, 4, 5, 7, 8))


def test_left_panel_mirrors_equalizer_in_rows_twenty_three_to_thirty_three() -> None:
    levels = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2 / 11)

    frame = render_left_panel(SystemMetrics(50, 50, 50), spectrum=levels).snapshot()

    column = lambda x: tuple(frame[y][x] for y in range(23, 34))
    assert column(8) == (220,) + (128,) * 10
    assert column(0) == (0,) * 9 + (220, 128)
    assert all(column(x) == (0,) * 11 for x in range(1, 8))


@pytest.mark.parametrize(
    ("blinking", "hidden_columns"),
    [
        ({"battery"}, (0, 1)),
        ({"cpu"}, (3, 4, 5)),
        ({"ram"}, (7, 8)),
    ],
)
def test_left_panel_blinks_only_the_critical_bar(
    blinking: set[str],
    hidden_columns: tuple[int, ...],
) -> None:
    metrics = SystemMetrics(battery_percent=100, cpu_percent=100, ram_used_percent=100)
    spectrum = (0.4,) * 9
    visible = render_left_panel(metrics, spectrum=spectrum, blinking=blinking, blink_on=True).snapshot()
    hidden = render_left_panel(metrics, spectrum=spectrum, blinking=blinking, blink_on=False).snapshot()

    assert hidden[21:] == visible[21:]
    assert all(hidden[y][column] == 0 for y in range(21) for column in hidden_columns)
    other_columns = {0, 1, 3, 4, 5, 7, 8} - set(hidden_columns)
    assert all(hidden[y][column] == visible[y][column] for y in range(21) for column in other_columns)


def test_system_metrics_looks_for_gpus_once_a_minute_or_when_one_vanishes(tmp_path: Path) -> None:
    drm = tmp_path / "drm"
    _gpu_card(drm, "card1", "16\n")
    now = [0.0]
    provider = SystemMetricsProvider(
        proc_stat_path=tmp_path / "missing-stat",
        meminfo_path=tmp_path / "missing-meminfo",
        battery_capacity_path=tmp_path / "missing-capacity",
        drm_root=drm,
        statvfs=_statvfs(blocks=0, free=0, available=0),
        clock=lambda: now[0],
        rescan_seconds=60,
    )
    assert provider.read().gpu_percent == 16

    _gpu_card(drm, "card2", "70\n")
    now[0] = 59.0
    assert provider.read().gpu_percent == 16  # the new card is not looked for yet
    now[0] = 60.0
    assert provider.read().gpu_percent == 70

    for card in ("card1", "card2"):
        (drm / card / "device" / "gpu_busy_percent").unlink()
    now[0] = 61.0
    assert provider.read().gpu_percent is None
    _gpu_card(drm, "card3", "33\n")
    now[0] = 62.0
    assert provider.read().gpu_percent == 33  # a vanished file triggered a new search
