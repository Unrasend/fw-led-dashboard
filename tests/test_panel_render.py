from __future__ import annotations

from pathlib import Path

import pytest

from framework_led_dashboard.alerts import AlertState
from framework_led_dashboard.frame import FRAME_WIDTH, FramePixels
from framework_led_dashboard.layout import (
    DEFAULT_LAYOUT,
    bars,
    equalizer,
    line,
    space,
    temperature,
)
from framework_led_dashboard.panel_render import (
    UNKNOWN_INTENSITY,
    VALUE_INTENSITY,
    PanelData,
    format_temperature,
    render_panel,
)
from framework_led_dashboard.system_metrics import SystemMetrics
from framework_led_dashboard.thermal import GpuTemperatureProvider
from framework_led_dashboard.weather import WeatherReading

METRICS = SystemMetrics(battery_percent=80, cpu_percent=35, ram_used_percent=60)


def rows(frame: FramePixels, first: int, last: int) -> list[str]:
    symbols = {0: ".", UNKNOWN_INTENSITY: "?", VALUE_INTENSITY: "#"}
    return ["".join(symbols.get(value, "+") for value in frame[y]) for y in range(first, last + 1)]


def test_widgets_stack_from_the_top_and_a_line_lights_a_full_row() -> None:
    data = PanelData(cpu_temperature_c=63)
    frame = render_panel((space(2), line(), temperature("cpu")), data).snapshot()
    alone = render_panel((temperature("cpu"),), data).snapshot()

    assert rows(frame, 0, 2) == [".........", ".........", "#########"]
    assert frame[3:8] == alone[0:5]
    assert rows(frame, 3, 3) == [".###.###."]  # "63", centred with a gap
    assert all(value == 0 for row in frame[8:] for value in row)


def test_outside_temperature_keeps_its_dot_and_shows_a_dim_dash_without_weather() -> None:
    widget = (temperature("outside"),)

    warm = render_panel(widget, PanelData(weather=WeatherReading(7.4, 0, True))).snapshot()
    unknown = render_panel(widget, PanelData()).snapshot()

    assert rows(warm, 0, 0) == ["..###...#"]
    assert rows(unknown, 2, 2) == [".???.???."]


def test_gpu_temperature_is_dim_while_the_gpu_sleeps_and_never_blinks() -> None:
    widget = (temperature("gpu"),)
    asleep = render_panel(widget, PanelData(gpu_temperature_c=None)).snapshot()
    hot = PanelData(gpu_temperature_c=99, alerts=AlertState(cpu_temperature=True))

    assert rows(asleep, 2, 2) == [".???.???."]
    assert (
        render_panel(widget, hot, blink_on=False).snapshot()
        == render_panel(widget, hot, blink_on=True).snapshot()
    )


def test_cpu_temperature_blinks_while_its_alert_is_on() -> None:
    widget = (space(3), temperature("cpu"))
    data = PanelData(cpu_temperature_c=121, alerts=AlertState(cpu_temperature=True))

    hidden = render_panel(widget, data, blink_on=False).snapshot()

    assert all(value == 0 for row in hidden for value in row)
    assert render_panel(widget, data, blink_on=True).snapshot() != hidden


@pytest.mark.parametrize(("value", "text"), [(None, "--"), (float("nan"), "--"), (63.4, "63"), (104.6, "105"), (-3, "0"), (1500, "999")])
def test_format_temperature(value: float | None, text: str) -> None:
    assert format_temperature(value) == text


def test_bars_scale_to_their_own_height() -> None:
    widget = (bars(("battery", "cpu", "ram"), 10),)

    frame = render_panel(widget, PanelData(metrics=METRICS)).snapshot()

    battery = sum(1 for y in range(10) if frame[y][0])
    cpu = sum(1 for y in range(10) if frame[y][4])
    assert (battery, cpu) == (8, 4)  # 80 % and 35 % of 10 rows
    assert all(value == 0 for row in frame[10:] for value in row)


def test_equalizer_fills_its_own_rows_and_mirrors_when_asked() -> None:
    widget = (space(4), equalizer(6))
    levels = (1.0,) + (0.0,) * (FRAME_WIDTH - 1)

    plain = render_panel(widget, PanelData(), spectrum=levels).snapshot()
    mirrored = render_panel(widget, PanelData(), spectrum=levels, mirrored=True).snapshot()

    assert [y for y in range(34) if plain[y][0]] == [4, 5, 6, 7, 8, 9]
    assert [y for y in range(34) if mirrored[y][FRAME_WIDTH - 1]] == [4, 5, 6, 7, 8, 9]
    assert render_panel(widget, PanelData(), spectrum=None).snapshot() == render_panel(
        (), PanelData()
    ).snapshot()


def test_the_default_layout_renders_on_both_panels() -> None:
    data = PanelData(
        metrics=METRICS,
        cpu_temperature_c=92,
        gpu_temperature_c=63,
        weather=WeatherReading(-12.0, 61, False),
    )
    left = render_panel(DEFAULT_LAYOUT.left, data, spectrum=(0.5,) * 9, mirrored=True).snapshot()
    right = render_panel(DEFAULT_LAYOUT.right, data, spectrum=(0.5,) * 9).snapshot()

    assert rows(left, 21, 21) == ["#########"]  # the line under the bars
    assert rows(left, 2, 2) == ["#..#..###"]  # -12: a 1-px minus, no degree dot
    assert any(right[y][x] for y in range(10) for x in range(FRAME_WIDTH))  # the icon


def _gpu_card(
    root: Path,
    card: str,
    *,
    boot_vga: str,
    status: str = "active",
    millidegrees: int = 50_000,
    label: str = "edge",
) -> Path:
    device = root / card / "device"
    hwmon = device / "hwmon" / "hwmon3"
    hwmon.mkdir(parents=True)
    (device / "boot_vga").write_text(boot_vga)
    (device / "power").mkdir()
    (device / "power" / "runtime_status").write_text(status)
    (hwmon / "temp1_input").write_text(str(millidegrees))
    (hwmon / "temp1_label").write_text(label)
    return hwmon / "temp1_input"


def test_gpu_temperature_prefers_the_discrete_gpu(tmp_path: Path) -> None:
    _gpu_card(tmp_path, "card1", boot_vga="1", millidegrees=63_000)
    _gpu_card(tmp_path, "card2", boot_vga="0", millidegrees=71_000)
    (tmp_path / "card1-eDP-1").mkdir()  # connectors are not cards

    assert GpuTemperatureProvider(drm_root=tmp_path).read_celsius() == 71.0


def test_gpu_temperature_falls_back_to_the_integrated_gpu(tmp_path: Path) -> None:
    _gpu_card(tmp_path, "card1", boot_vga="1", millidegrees=63_000)

    assert GpuTemperatureProvider(drm_root=tmp_path).read_celsius() == 63.0


def test_gpu_temperature_does_not_read_a_sleeping_gpu(tmp_path: Path) -> None:
    sensor = _gpu_card(tmp_path, "card2", boot_vga="0", status="suspended")
    sensor.unlink()  # reading it would fail loudly; it must not even be tried
    sensor.mkdir()

    assert GpuTemperatureProvider(drm_root=tmp_path).read_celsius() is None


def test_gpu_temperature_prefers_edge_and_looks_again_after_a_failed_read(tmp_path: Path) -> None:
    junction = _gpu_card(tmp_path, "card1", boot_vga="1", millidegrees=80_000, label="junction")
    edge = junction.with_name("temp2_input")
    edge.write_text("61000")
    edge.with_name("temp2_label").write_text("edge")
    now = [0.0]
    provider = GpuTemperatureProvider(drm_root=tmp_path, clock=lambda: now[0])

    assert provider.read_celsius() == 61.0
    edge.unlink()
    assert provider.read_celsius() is None  # gone: rescan next time
    assert provider.read_celsius() == 80.0


def test_gpu_temperature_without_any_gpu_is_unknown(tmp_path: Path) -> None:
    assert GpuTemperatureProvider(drm_root=tmp_path).read_celsius() is None
    assert GpuTemperatureProvider(drm_root=tmp_path / "missing").read_celsius() is None
