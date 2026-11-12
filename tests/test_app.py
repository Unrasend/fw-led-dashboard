from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from framework_led_dashboard.__main__ import main
from framework_led_dashboard.alerts import AlertPolicy, AlertState
from framework_led_dashboard.app import (
    BLANK_FRAME,
    DISABLED_POLL_SECONDS,
    FRAME_RESEND_SECONDS,
    SLEEP_REFRESH_SECONDS,
    Dashboard,
    DashboardSnapshot,
    DashboardState,
    Loop,
    MicrophoneWatch,
    PanelWorker,
    SensorSampler,
    WeatherUpdater,
    panel_targets,
    render_left,
    render_right,
    run_dashboard,
    side_renderer,
)
from framework_led_dashboard.config import AlertsConfig, ConfigError, DashboardConfig, parse_config
from framework_led_dashboard.control import ControlError, DisplaySettings
from framework_led_dashboard.left_panel import render_left_panel
from framework_led_dashboard.location import Coordinates, LocationSource
from framework_led_dashboard.matrix_transport import (
    ReconnectPending,
    TransportError,
    brightness_command,
    sleep_command,
)
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.system_metrics import SystemMetrics
from framework_led_dashboard.weather import WeatherProvider, WeatherReading

PRAGUE = Coordinates(50.08, 14.43, LocationSource.IP)
CLEAR_NIGHT = WeatherReading(-1.0, 0, False)
METRICS = SystemMetrics(battery_percent=78, cpu_percent=35, ram_used_percent=62)
LEVELS = (0.3,) * 9
CONFIGURED = parse_config({"panels": {"left": {"identity": "L"}, "right": {"identity": "R"}}})
ON = DisplaySettings(40)
OFF = DisplaySettings(40, enabled=False)


def always(settings: DisplaySettings):
    return lambda: settings


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeTransport:
    """Raise the scripted outcomes in order (None = success), then accept everything."""

    def __init__(
        self,
        outcomes: tuple[Exception | None, ...] = (),
        *,
        command_outcomes: tuple[Exception | None, ...] = (),
    ) -> None:
        self.frames: list[object] = []
        self.commands: list[bytes] = []
        self.closed = False
        self._outcomes = list(outcomes)
        self._command_outcomes = list(command_outcomes)

    def write_frame(self, frame: object) -> None:
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if outcome is not None:
                raise outcome
        self.frames.append(frame)

    def send(self, command: bytes) -> None:
        if self._command_outcomes:
            outcome = self._command_outcomes.pop(0)
            if outcome is not None:
                raise outcome
        self.commands.append(command)

    def close(self) -> None:
        self.closed = True


class FakeMicrophone:
    def __init__(self) -> None:
        self.current: tuple[float, ...] | None = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def levels(self) -> tuple[float, ...] | None:
        return self.current


class ScriptedWeather:
    def __init__(self, *outcomes: WeatherReading | Exception | None) -> None:
        self.locations: list[Coordinates] = []
        self._outcomes = list(outcomes)

    def __call__(self, location: Coordinates) -> WeatherReading | None:
        self.locations.append(location)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _weather_updater(
    state: DashboardState,
    fetch: ScriptedWeather,
    wall_clock: FakeClock,
    *,
    location: Coordinates | None = PRAGUE,
) -> WeatherUpdater:
    return WeatherUpdater(
        state,
        resolve_location=lambda: location,
        provider=WeatherProvider(fetch_weather=fetch, clock=wall_clock),
        interval_seconds=900,
        stale_after_seconds=7200,
        retry_initial_seconds=30,
        wall_clock=wall_clock,
    )


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def test_panel_targets_send_each_side_to_its_configured_identity() -> None:
    assert panel_targets(CONFIGURED) == {"left": "L", "right": "R"}


def test_panel_targets_stay_physical_when_the_config_swaps_panels() -> None:
    # The configured swap is only the starting value of the live setting (side_renderer).
    swapped = parse_config(
        {"swap_panels": True, "panels": {"left": {"identity": "L"}, "right": {"identity": "R"}}}
    )

    assert panel_targets(swapped) == {"left": "L", "right": "R"}


def test_side_renderer_swaps_content_as_soon_as_the_setting_changes() -> None:
    current = {"settings": DisplaySettings(40)}
    bars_frame = ((1,),)
    weather_frame = ((2,),)
    renderers = {
        side: side_renderer(
            side,
            settings=lambda: current["settings"],
            left=lambda _now: bars_frame,
            right=lambda _now: weather_frame,
        )
        for side in ("left", "right")
    }

    assert renderers["left"](0.0) == bars_frame
    assert renderers["right"](0.0) == weather_frame

    current["settings"] = DisplaySettings(40, swap_panels=True)

    assert renderers["left"](0.1) == weather_frame
    assert renderers["right"](0.1) == bars_frame


def test_side_renderer_rejects_an_unknown_side() -> None:
    with pytest.raises(ValueError, match="side"):
        side_renderer(
            "middle", settings=lambda: DisplaySettings(40), left=lambda _n: (), right=lambda _n: ()
        )


def test_panel_targets_require_both_identities() -> None:
    with pytest.raises(ConfigError, match="identity"):
        panel_targets(parse_config({"panels": {"left": {"identity": "L"}}}))


def test_left_render_uses_snapshot_spectrum_and_blink_phase() -> None:
    alerts = AlertState(battery=True)
    snapshot = DashboardSnapshot(metrics=METRICS, alerts=alerts)

    shown = render_left(snapshot, LEVELS, 0.2)
    hidden = render_left(snapshot, LEVELS, 0.7)

    expected_shown = render_left_panel(METRICS, spectrum=LEVELS, alerts=alerts, blink_on=True)
    expected_hidden = render_left_panel(METRICS, spectrum=LEVELS, alerts=alerts, blink_on=False)
    assert shown == expected_shown.snapshot()
    assert hidden == expected_hidden.snapshot()
    assert shown != hidden


def test_left_render_uses_the_bars_chosen_in_settings() -> None:
    metrics = SystemMetrics(battery_percent=10, cpu_percent=20, ram_used_percent=30, gpu_percent=90)
    snapshot = DashboardSnapshot(metrics=metrics)

    actual = render_left(snapshot, None, 0.2, bars=("gpu", "cpu", "ram"))

    expected = render_left_panel(metrics, bars=("gpu", "cpu", "ram"), spectrum=None)
    assert actual == expected.snapshot()
    assert actual != render_left(snapshot, None, 0.2)


def test_right_render_animates_weather_at_two_fps_and_blinks_cpu_temperature() -> None:
    alerts = AlertState(cpu_temperature=True)
    snapshot = DashboardSnapshot(cpu_temperature_c=120, weather=CLEAR_NIGHT, alerts=alerts)

    actual = render_right(snapshot, None, 1.7)

    expected = render_right_panel(
        cpu_temperature_c=120,
        weather=CLEAR_NIGHT,
        weather_frame=3,
        spectrum=None,
        alerts=alerts,
        blink_on=False,
    )
    assert actual == expected.snapshot()


def test_sensor_sampler_publishes_metrics_and_alerts_without_touching_weather() -> None:
    state = DashboardState()
    state.update(weather=CLEAR_NIGHT)
    clock = FakeClock()
    sampler = SensorSampler(
        state,
        read_metrics=lambda: SystemMetrics(battery_percent=8, cpu_percent=35, ram_used_percent=96),
        read_cpu_temperature=lambda: 121.0,
        alert_policy=AlertPolicy(AlertsConfig()),
        interval_seconds=1,
        clock=clock,
    )

    assert sampler.step() == 1
    snapshot = state.snapshot()
    assert snapshot.metrics.battery_percent == 8
    assert snapshot.cpu_temperature_c == 121.0
    assert snapshot.weather == CLEAR_NIGHT
    assert snapshot.alerts == AlertState(battery=True, cpu_temperature=True)

    clock.now = 10.0
    sampler.step()
    assert state.snapshot().alerts.ram is True


def test_sensor_sampler_feeds_outside_temperature_to_alert_policy() -> None:
    state = DashboardState()
    state.update(weather=WeatherReading(-25.0, 0, False))
    sampler = SensorSampler(
        state,
        read_metrics=lambda: METRICS,
        read_cpu_temperature=lambda: None,
        alert_policy=AlertPolicy(AlertsConfig(outside_temperature_min_c=-20)),
        interval_seconds=1,
    )

    sampler.step()

    assert state.snapshot().alerts == AlertState(outside_temperature=True)


def test_weather_updater_publishes_reading_and_waits_full_interval() -> None:
    state = DashboardState()
    fetch = ScriptedWeather(CLEAR_NIGHT)

    assert _weather_updater(state, fetch, FakeClock(1000.0)).step() == 900
    assert state.snapshot().weather == CLEAR_NIGHT
    assert fetch.locations == [PRAGUE]


def test_weather_updater_retries_with_backoff_and_keeps_last_good(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = DashboardState()
    recovered = WeatherReading(2.0, 0, False)
    failures_in_a_row = (None, OSError("offline"), None, None, None, None)
    fetch = ScriptedWeather(CLEAR_NIGHT, *failures_in_a_row, recovered)
    updater = _weather_updater(state, fetch, FakeClock(1000.0))

    with caplog.at_level(logging.INFO):
        assert updater.step() == 900
        failures = [updater.step() for _ in range(6)]
        assert state.snapshot().weather == CLEAR_NIGHT
        assert updater.step() == 900

    assert failures == [30, 60, 120, 240, 480, 900]
    assert state.snapshot().weather == recovered
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "recovered" in caplog.text


def test_weather_updater_hides_reading_older_than_stale_limit() -> None:
    state = DashboardState()
    wall_clock = FakeClock(1000.0)
    updater = _weather_updater(state, ScriptedWeather(CLEAR_NIGHT, None), wall_clock)

    updater.step()
    wall_clock.now = 1000.0 + 7201
    updater.step()

    assert state.snapshot().weather is None


def test_weather_updater_without_location_skips_fetch_and_retries() -> None:
    state = DashboardState()
    fetch = ScriptedWeather()

    assert _weather_updater(state, fetch, FakeClock(), location=None).step() == 30
    assert fetch.locations == []
    assert state.snapshot().weather is None


def test_panel_worker_writes_rendered_frame_and_paces_to_max_fps() -> None:
    clock = FakeClock()
    transport = FakeTransport()
    rendered_at: list[float] = []

    def render(now: float) -> str:
        rendered_at.append(now)
        clock.now += 0.03
        return "frame"

    worker = PanelWorker("left", transport, render, settings=always(ON), max_fps=10, clock=clock)

    assert worker.step() == pytest.approx(0.07)
    assert transport.frames == ["frame"]
    assert rendered_at == [0.0]


def test_panel_worker_does_not_sleep_when_the_panel_is_slower_than_max_fps() -> None:
    clock = FakeClock()

    def render(_now: float) -> str:
        clock.now += 0.15
        return "frame"

    worker = PanelWorker(
        "left", FakeTransport(), render, settings=always(ON), max_fps=10, clock=clock
    )

    assert worker.step() == 0.0


def test_panel_worker_waits_out_reconnect_backoff() -> None:
    transport = FakeTransport((ReconnectPending(1.5),))

    worker = PanelWorker("right", transport, lambda _now: "frame", settings=always(ON), max_fps=10)

    assert worker.step() == 1.5
    assert transport.frames == []


def test_panel_worker_logs_loss_once_and_recovery(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport((TransportError("gone"), TransportError("gone"), None))
    worker = PanelWorker("left", transport, lambda _now: "frame", settings=always(ON), max_fps=10)

    with caplog.at_level(logging.INFO):
        assert worker.step() == 0.0
        assert worker.step() == 0.0
        worker.step()

    assert _messages(caplog) == ["left panel unavailable: gone", "left panel connected"]


def test_panel_worker_applies_each_brightness_change_once() -> None:
    settings = [DisplaySettings(40)]
    transport = FakeTransport()
    worker = PanelWorker(
        "left", transport, lambda _now: "frame", settings=lambda: settings[0], max_fps=10
    )

    worker.step()
    worker.step()
    settings[0] = DisplaySettings(70)
    worker.step()
    worker.step()

    assert transport.commands == [brightness_command(40), brightness_command(70)]
    assert transport.frames == ["frame"]  # unchanged frames are not resent


def test_panel_worker_sends_only_changed_frames_plus_a_periodic_resend() -> None:
    clock = FakeClock()
    transport = FakeTransport()
    frames = iter(["a", "a", "b", "b", "b", "b"])
    worker = PanelWorker(
        "left", transport, lambda _now: next(frames), settings=always(ON), max_fps=10, clock=clock
    )

    for now in (0.0, 0.1, 0.2, 0.3, 2.1, 2.2):
        clock.now = now
        worker.step()

    # "a" once; "b" when it changes and again FRAME_RESEND_SECONDS after it was sent.
    assert FRAME_RESEND_SECONDS == 2.0
    assert transport.frames == ["a", "b", "b"]


def test_panel_worker_notices_a_lost_panel_at_the_resend_and_redraws_it_at_once() -> None:
    clock = FakeClock()
    # The first write lands; the panel then goes away, which the periodic resend finds out.
    transport = FakeTransport((None, TransportError("gone")))
    worker = PanelWorker(
        "left", transport, lambda _now: "frame", settings=always(ON), max_fps=10, clock=clock
    )

    for now in (0.0, 1.0, 2.0, 2.1):
        clock.now = now
        worker.step()

    # 0.0 sent, 1.0 skipped, 2.0 resend fails, 2.1 the reconnected panel gets it right away.
    assert transport.frames == ["frame", "frame"]


def test_panel_worker_sleeps_the_panel_while_off_and_resumes_frames_when_on() -> None:
    settings = [OFF]
    clock = FakeClock()
    transport = FakeTransport()
    worker = PanelWorker(
        "right",
        transport,
        lambda _now: "frame",
        settings=lambda: settings[0],
        max_fps=10,
        clock=clock,
    )

    assert worker.step() == DISABLED_POLL_SECONDS
    clock.now = SLEEP_REFRESH_SECONDS - 0.1
    assert worker.step() == DISABLED_POLL_SECONDS
    assert transport.commands == [sleep_command(True)]
    assert transport.frames == []

    # Repeat the (idempotent) sleep so a panel that woke on its own, e.g. after resume, goes dark.
    clock.now = SLEEP_REFRESH_SECONDS
    worker.step()
    assert transport.commands == [sleep_command(True)] * 2

    settings[0] = ON
    worker.step()
    assert transport.frames == ["frame"]
    assert transport.commands == [sleep_command(True)] * 2 + [brightness_command(40)]


def test_panel_worker_retries_sleep_after_a_lost_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport(command_outcomes=(TransportError("gone"),))
    worker = PanelWorker("left", transport, lambda _now: "frame", settings=always(OFF), max_fps=10)

    with caplog.at_level(logging.INFO):
        assert worker.step() == 0.0
        assert worker.step() == DISABLED_POLL_SECONDS

    assert transport.commands == [sleep_command(True)]
    assert _messages(caplog) == ["left panel unavailable: gone", "left panel connected"]


def test_panel_worker_shutdown_blanks_and_closes_a_lit_panel() -> None:
    transport = FakeTransport()
    worker = PanelWorker("left", transport, lambda _now: "frame", settings=always(ON), max_fps=10)

    worker.shutdown()

    assert transport.frames == [BLANK_FRAME]
    assert transport.closed is True


def test_panel_worker_shutdown_does_not_wake_a_panel_that_is_off() -> None:
    transport = FakeTransport()
    worker = PanelWorker("left", transport, lambda _now: "frame", settings=always(OFF), max_fps=10)

    worker.shutdown()

    assert transport.frames == []
    assert transport.closed is True


def test_panel_worker_shutdown_tolerates_an_unreachable_panel() -> None:
    transport = FakeTransport((ReconnectPending(3.0),))
    worker = PanelWorker("right", transport, lambda _now: "frame", settings=always(ON), max_fps=10)

    worker.shutdown()

    assert transport.frames == []
    assert transport.closed is True


def test_microphone_watch_logs_only_availability_changes(caplog: pytest.LogCaptureFixture) -> None:
    microphone = FakeMicrophone()
    watch = MicrophoneWatch(microphone, interval_seconds=1.0, startup_grace_seconds=0)

    with caplog.at_level(logging.INFO):
        for current in (None, None, LEVELS, LEVELS, None):
            microphone.current = current
            assert watch.step() == 1.0

    assert _messages(caplog) == [
        "microphone unavailable, equalizer dark",
        "microphone available",
        "microphone unavailable, equalizer dark",
    ]


def test_microphone_watch_releases_the_microphone_while_the_panels_are_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    microphone = FakeMicrophone()
    microphone.current = LEVELS
    panels_on = [True]
    now = [0.0]
    watch = MicrophoneWatch(
        microphone,
        wanted=lambda: panels_on[0],
        startup_grace_seconds=2.0,
        clock=lambda: now[0],
    )

    with caplog.at_level(logging.INFO):
        now[0] = 2.0
        watch.step()  # running since Dashboard.start(), past its grace: judged
        panels_on[0] = False
        now[0] = 3.0
        watch.step()
        assert microphone.stopped is True
        microphone.current = None  # stopped: no levels, but no "unavailable" either
        now[0] = 9.0
        watch.step()
        microphone.started = False
        panels_on[0] = True
        now[0] = 10.0
        watch.step()  # restarted, within its grace: not judged yet
        assert microphone.started is True
        now[0] = 12.0
        microphone.current = LEVELS
        watch.step()

    assert _messages(caplog) == [
        "microphone available",
        "panels off: microphone released",
        "panels on: microphone capture resumed",
        "microphone available",
    ]


def test_microphone_watch_gives_a_slow_capture_its_grace_before_calling_it_dark(
    caplog: pytest.LogCaptureFixture,
) -> None:
    microphone = FakeMicrophone()
    now = [0.0]
    watch = MicrophoneWatch(microphone, startup_grace_seconds=2.0, clock=lambda: now[0])

    with caplog.at_level(logging.INFO):
        now[0] = 1.0
        watch.step()
        now[0] = 2.0
        watch.step()

    assert _messages(caplog) == ["microphone unavailable, equalizer dark"]


def test_dashboard_stop_shuts_panels_down_after_their_loops_end() -> None:
    microphone = FakeMicrophone()
    stepped = threading.Event()
    events: list[str] = []

    def step() -> float:
        stepped.set()
        return 0.01

    dashboard = Dashboard(
        panels=((Loop("panel-left", step), lambda: events.append("panel shutdown")),),
        background=(Loop("sensors", lambda: 0.01),),
        microphone=microphone,
        on_stop=(lambda: events.append("control closed"),),
    )
    dashboard.start()
    assert stepped.wait(1.0)
    dashboard.stop()

    assert microphone.started is True
    assert microphone.stopped is True
    assert events == ["panel shutdown", "control closed"]


def test_run_dashboard_stops_cleanly_on_sigterm_and_restores_handlers() -> None:
    events: list[str] = []
    previous_handler = signal.getsignal(signal.SIGTERM)

    class RecordingDashboard:
        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    def dashboard_factory(config: DashboardConfig) -> RecordingDashboard:
        assert config is CONFIGURED
        return RecordingDashboard()

    timer = threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGTERM))
    timer.start()
    started = time.monotonic()
    try:
        assert run_dashboard(CONFIGURED, dashboard_factory=dashboard_factory) == 0
    finally:
        timer.cancel()

    assert events == ["start", "stop"]
    assert time.monotonic() - started < 2.0
    assert signal.getsignal(signal.SIGTERM) == previous_handler


def test_run_dashboard_refuses_to_start_next_to_a_running_dashboard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def dashboard_factory(_config: DashboardConfig) -> None:
        raise ControlError("another dashboard is already listening on /run/user/1000/x.sock")

    with caplog.at_level(logging.ERROR):
        assert run_dashboard(CONFIGURED, dashboard_factory=dashboard_factory) == 1

    assert "already listening" in caplog.text


def test_cli_tray_starts_the_tray_without_a_config() -> None:
    started: list[bool] = []

    def tray_runner() -> int:
        started.append(True)
        return 0

    assert main(["--tray"], tray_runner=tray_runner) == 0
    assert started == [True]


def test_cli_run_hands_loaded_config_to_the_dashboard(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[panels.left]\nidentity = "L"\n\n[panels.right]\nidentity = "R"\n', encoding="utf-8"
    )
    received: list[DashboardConfig] = []

    def runner(config: DashboardConfig) -> int:
        received.append(config)
        return 0

    assert main(["--run", str(config_path)], dashboard_runner=runner) == 0
    assert panel_targets(received[0]) == {"left": "L", "right": "R"}


def test_cli_run_refuses_config_without_panel_identities(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    assert main(["--run", str(config_path)], dashboard_runner=lambda _config: 0) == 2
    assert "identity" in capsys.readouterr().err
