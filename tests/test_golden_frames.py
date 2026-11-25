from __future__ import annotations

from pathlib import Path

from framework_led_dashboard.alerts import AlertState
from framework_led_dashboard.equalizer import EQUALIZER_BOTTOM, EQUALIZER_ROWS, EQUALIZER_TOP
from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from framework_led_dashboard.left_panel import render_left_panel
from framework_led_dashboard.matrix_transport import MatrixTransport
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.system_metrics import SystemMetrics
from framework_led_dashboard.weather import WeatherReading

GOLDEN_DIRECTORY = Path(__file__).parent / "goldens"
PIXEL_VALUES = {".": 0, "L": 128, "#": 220, "?": 24}
LEFT_METRICS = SystemMetrics(battery_percent=78, cpu_percent=35, ram_used_percent=62)


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


def _spectrum_from_golden(golden: FramePixels, *, mirrored: bool = False) -> tuple[float, ...]:
    heights = [sum(1 for y in range(EQUALIZER_TOP, EQUALIZER_BOTTOM + 1) if golden[y][x]) for x in range(FRAME_WIDTH)]
    ordered = list(reversed(heights)) if mirrored else heights
    return tuple(height / EQUALIZER_ROWS for height in ordered)


def _render_left(golden: FramePixels, *, blinking: frozenset[str] = frozenset(), blink_on: bool = True) -> FramePixels:
    spectrum = _spectrum_from_golden(golden, mirrored=True)
    return render_left_panel(LEFT_METRICS, spectrum=spectrum, blinking=blinking, blink_on=blink_on).snapshot()


def _render_right(
    golden: FramePixels,
    *,
    weather_code: int | None = 0,
    is_day: bool = True,
    frame_index: int = 0,
    cpu_temperature_c: float | None = 105,
    alerts: AlertState | None = None,
    blink_on: bool = True,
) -> FramePixels:
    weather = None if weather_code is None else WeatherReading(29.0, weather_code, is_day)
    return render_right_panel(
        weather=weather,
        weather_frame=frame_index,
        cpu_temperature_c=cpu_temperature_c,
        spectrum=_spectrum_from_golden(golden),
        alerts=alerts,
        blink_on=blink_on,
    ).snapshot()


def _expected_usb_commands(frame: FramePixels) -> list[bytes]:
    stage_commands = [
        bytes([0x32, 0xAC, 0x07, x, *[frame[y][x] for y in range(FRAME_HEIGHT)]]) for x in range(FRAME_WIDTH)
    ]
    return [*stage_commands, bytes([0x32, 0xAC, 0x08, 0x00])]


def _cases() -> tuple[tuple[str, FramePixels, FramePixels], ...]:
    left = _load_golden("left_dashboard.txt")
    left_alert = _load_golden("left_alert_battery_off.txt")
    local = _load_golden("right_local_metrics.txt")
    showers = _load_golden("right_weather_showers_day_0.txt")
    clear_0 = _load_golden("right_weather_clear_0.txt")
    clear_1 = _load_golden("right_weather_clear_1.txt")
    partly_cloudy_night = _load_golden("right_weather_partly_cloudy_night_0.txt")
    heavy_rain = _load_golden("right_weather_heavy_rain_day_0.txt")
    thunder_hail = _load_golden("right_weather_thunder_hail_night_1.txt")
    cpu_alert = _load_golden("right_alert_cpu_off.txt")
    return (
        ("left", _render_left(left), left),
        ("left-battery-alert-off", _render_left(left_alert, blinking=frozenset({"battery"}), blink_on=False), left_alert),
        ("right-local", _render_right(local, weather_code=None, cpu_temperature_c=54), local),
        ("right-showers-day-0", _render_right(showers, weather_code=80), showers),
        ("right-clear-0", _render_right(clear_0), clear_0),
        ("right-clear-1", _render_right(clear_1, frame_index=1), clear_1),
        ("right-partly-cloudy-night-0", _render_right(partly_cloudy_night, weather_code=2, is_day=False), partly_cloudy_night),
        ("right-heavy-rain-day-0", _render_right(heavy_rain, weather_code=65), heavy_rain),
        ("right-thunder-hail-night-1", _render_right(thunder_hail, weather_code=99, is_day=False, frame_index=1), thunder_hail),
        (
            "right-cpu-alert-off",
            _render_right(cpu_alert, cpu_temperature_c=120, alerts=AlertState(cpu_temperature=True), blink_on=False),
            cpu_alert,
        ),
    )


def test_every_golden_frame_matches_all_306_mockup_pixels() -> None:
    for identity, actual, expected in _cases():
        assert sum(len(row) for row in expected) == 306, identity
        assert actual == expected, identity


def test_mockup_frames_reach_serial_as_exact_usb_bytes() -> None:
    for identity, rendered, golden in _cases():
        serial = RecordingSerial()
        transport = MatrixTransport(
            identity=identity,
            path_resolver=lambda panel_identity: f"/dev/{panel_identity}",
            serial_factory=lambda path, connection=serial: connection,
        )

        assert rendered == golden
        transport.write_frame(rendered)

        assert serial.writes == _expected_usb_commands(golden)
        transport.close()
        assert serial.closed is True
