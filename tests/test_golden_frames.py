from __future__ import annotations

from pathlib import Path

from framework_led_dashboard.alerts import AlertState
from framework_led_dashboard.battery import BatteryMode, BatteryPower
from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from framework_led_dashboard.left_panel import render_left_panel
from framework_led_dashboard.matrix_transport import MatrixTransport
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.system_metrics import SystemMetrics
from framework_led_dashboard.weather import WeatherReading

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


class RecordingSerial:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def read(self, size: int) -> bytes:
        return bytes(size)

    def close(self) -> None:
        self.closed = True


def _load_golden(name: str) -> FramePixels:
    lines = (GOLDEN_DIRECTORY / name).read_text(encoding="utf-8").splitlines()
    assert len(lines) == FRAME_HEIGHT
    assert all(len(line) == FRAME_WIDTH for line in lines)
    assert all(pixel in PIXEL_VALUES for line in lines for pixel in line)
    return tuple(tuple(PIXEL_VALUES[pixel] for pixel in line) for line in lines)


def _render_left_mockup_example() -> FramePixels:
    life = tuple(tuple(pixel == "1" for pixel in row) for row in LEFT_LIFE_INPUT)
    return render_left_panel(
        life,
        SystemMetrics(battery_percent=72, cpu_percent=38, ram_used_percent=61),
    ).snapshot()


def _render_right_mockup_example() -> FramePixels:
    return render_right_panel(
        cpu_temperature_c=54,
        battery_power=BatteryPower(BatteryMode.DISCHARGING, 12.4),
    ).snapshot()


def _render_right_weather_example(
    frame_index: int,
    *,
    weather_code: int = 0,
    is_day: bool = True,
) -> FramePixels:
    return render_right_panel(
        weather=WeatherReading(
            temperature_c=18.4,
            weather_code=weather_code,
            is_day=is_day,
        ),
        weather_frame=frame_index,
        cpu_temperature_c=54,
        battery_power=BatteryPower(BatteryMode.DISCHARGING, 12.4),
    ).snapshot()


def _render_left_battery_alert_off() -> FramePixels:
    life = tuple(tuple(pixel == "1" for pixel in row) for row in LEFT_LIFE_INPUT)
    return render_left_panel(
        life,
        SystemMetrics(battery_percent=72, cpu_percent=38, ram_used_percent=61),
        alerts=AlertState(battery=True),
        blink_on=False,
    ).snapshot()


def _render_right_cpu_alert_off() -> FramePixels:
    return render_right_panel(
        weather=WeatherReading(temperature_c=18.4, weather_code=0, is_day=True),
        cpu_temperature_c=120,
        battery_power=BatteryPower(BatteryMode.DISCHARGING, 12.4),
        alerts=AlertState(cpu_temperature=True),
        blink_on=False,
    ).snapshot()


def _expected_usb_commands(frame: FramePixels) -> tuple[bytes, ...]:
    stage_commands = tuple(
        bytes([0x32, 0xAC, 0x07, x, *[frame[y][x] for y in range(FRAME_HEIGHT)]])
        for x in range(FRAME_WIDTH)
    )
    return (*stage_commands, bytes([0x32, 0xAC, 0x08, 0x00]))


def test_left_panel_matches_all_306_mockup_pixels() -> None:
    actual = _render_left_mockup_example()
    expected = _load_golden("left_dashboard.txt")

    assert sum(len(row) for row in expected) == 306
    assert actual == expected


def test_right_panel_local_widgets_match_all_306_mockup_pixels() -> None:
    actual = _render_right_mockup_example()
    expected = _load_golden("right_local_metrics.txt")

    assert sum(len(row) for row in expected) == 306
    assert actual == expected


def test_right_panel_weather_animation_matches_all_306_mockup_pixels() -> None:
    cases = (
        (_render_right_weather_example(0), _load_golden("right_weather_clear_0.txt")),
        (_render_right_weather_example(1), _load_golden("right_weather_clear_1.txt")),
        (
            _render_right_weather_example(0, weather_code=2, is_day=False),
            _load_golden("right_weather_partly_cloudy_night_0.txt"),
        ),
        (
            _render_right_weather_example(0, weather_code=65),
            _load_golden("right_weather_heavy_rain_day_0.txt"),
        ),
        (
            _render_right_weather_example(1, weather_code=99, is_day=False),
            _load_golden("right_weather_thunder_hail_night_1.txt"),
        ),
    )

    for actual, expected in cases:
        assert sum(len(row) for row in expected) == 306
        assert actual == expected


def test_alert_blink_off_frames_match_all_306_mockup_pixels() -> None:
    cases = (
        (_render_left_battery_alert_off(), _load_golden("left_alert_battery_off.txt")),
        (_render_right_cpu_alert_off(), _load_golden("right_alert_cpu_off.txt")),
    )

    for actual, expected in cases:
        assert sum(len(row) for row in expected) == 306
        assert actual == expected


def test_mockup_frames_reach_serial_as_exact_usb_bytes() -> None:
    cases = (
        ("left", _render_left_mockup_example(), _load_golden("left_dashboard.txt")),
        (
            "right",
            _render_right_mockup_example(),
            _load_golden("right_local_metrics.txt"),
        ),
        (
            "right-clear-0",
            _render_right_weather_example(0),
            _load_golden("right_weather_clear_0.txt"),
        ),
        (
            "right-clear-1",
            _render_right_weather_example(1),
            _load_golden("right_weather_clear_1.txt"),
        ),
        (
            "right-partly-cloudy-night",
            _render_right_weather_example(0, weather_code=2, is_day=False),
            _load_golden("right_weather_partly_cloudy_night_0.txt"),
        ),
        (
            "right-heavy-rain-day",
            _render_right_weather_example(0, weather_code=65),
            _load_golden("right_weather_heavy_rain_day_0.txt"),
        ),
        (
            "right-thunder-hail-night",
            _render_right_weather_example(1, weather_code=99, is_day=False),
            _load_golden("right_weather_thunder_hail_night_1.txt"),
        ),
        (
            "left-battery-alert-off",
            _render_left_battery_alert_off(),
            _load_golden("left_alert_battery_off.txt"),
        ),
        (
            "right-cpu-alert-off",
            _render_right_cpu_alert_off(),
            _load_golden("right_alert_cpu_off.txt"),
        ),
    )

    for identity, rendered, golden in cases:
        serial = RecordingSerial()
        transport = MatrixTransport(
            identity=identity,
            path_resolver=lambda panel_identity: f"/dev/{panel_identity}",
            serial_factory=lambda path, connection=serial: connection,
        )

        assert rendered == golden
        transport.write_frame(rendered)

        assert serial.writes == list(_expected_usb_commands(golden))
        transport.close()
        assert serial.closed is True
