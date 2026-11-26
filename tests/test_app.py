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
    EXIT_PORTS_CHANGED,
    FRAME_RESEND_SECONDS,
    SLEEP_REFRESH_SECONDS,
    Dashboard,
    DashboardSnapshot,
    DashboardState,
    Loop,
    PanelWorker,
    PortWatch,
    SensorSampler,
    SoundWatch,
    WeatherUpdater,
    panel_targets,
    render_side,
    run_dashboard,
    wanted_capture,
)
from framework_led_dashboard.audio import CaptureRequest, Spectrum
from framework_led_dashboard.config import AlertsConfig, ConfigError, DashboardConfig, parse_config
from framework_led_dashboard.control import ControlError, DisplaySettings
from framework_led_dashboard.layout import (
    BlinkRule,
    Layout,
    bars,
    classic_left,
    classic_right,
    equalizer,
    temperature,
    weather,
)
from framework_led_dashboard.left_panel import render_left_panel
from framework_led_dashboard.location import Coordinates, LocationSource
from framework_led_dashboard.matrix_transport import (
    ReconnectPending,
    TransportError,
    brightness_command,
    sleep_command,
)
from framework_led_dashboard.panel_render import PanelData, render_panel
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.system_metrics import SystemMetrics
from framework_led_dashboard.weather import WeatherProvider, WeatherReading

PRAGUE = Coordinates(50.08, 14.43, LocationSource.IP)
CLEAR_NIGHT = WeatherReading(-1.0, 0, False)
METRICS = SystemMetrics(battery_percent=78, cpu_percent=35, ram_used_percent=62)
LEVELS = (0.3,) * 9
MONO = Spectrum(LEVELS, LEVELS)
CLASSIC = Layout(left=classic_left(), right=classic_right())
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


class FakeSound:
    def __init__(self) -> None:
        self.current: Spectrum | None = None
        self.requests: list[CaptureRequest] = []
        self.stopped = False

    def start(self, request: CaptureRequest) -> None:
        self.requests.append(request)

    def stop(self) -> None:
        self.stopped = True

    def levels(self) -> Spectrum | None:
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
    # The configured swap is only the starting value of the live setting (render_side).
    swapped = parse_config(
        {"swap_panels": True, "panels": {"left": {"identity": "L"}, "right": {"identity": "R"}}}
    )

    assert panel_targets(swapped) == {"left": "L", "right": "R"}


def test_panel_targets_require_both_identities() -> None:
    with pytest.raises(ConfigError, match="identity"):
        panel_targets(parse_config({"panels": {"left": {"identity": "L"}}}))


def test_render_side_draws_the_classic_layout_like_the_old_panels() -> None:
    alerts = AlertState(cpu_temperature=True)
    battery = (("battery", BlinkRule("below", 10)),)
    snapshot = DashboardSnapshot(
        metrics=METRICS,
        cpu_temperature_c=120,
        weather=CLEAR_NIGHT,
        alerts=alerts,
        blinking_bars=frozenset(battery),
    )
    settings = DisplaySettings(40, layout=CLASSIC)

    for elapsed, visible in ((0.2, True), (0.7, False)):
        left = render_side("left", settings, snapshot, MONO, elapsed)
        assert left == render_left_panel(
            METRICS, spectrum=LEVELS, blinking={"battery"}, blink_on=visible
        ).snapshot()
    right = render_side("right", settings, snapshot, None, 1.7)
    assert right == render_right_panel(
        cpu_temperature_c=120, weather=CLEAR_NIGHT, weather_frame=3, alerts=alerts, blink_on=False
    ).snapshot()


def test_render_side_swaps_the_widgets_but_keeps_each_modules_channel_and_mirroring() -> None:
    snapshot = DashboardSnapshot(metrics=METRICS, weather=CLEAR_NIGHT)
    stereo = Spectrum((0.9,) + (0.0,) * 8, (0.0,) * 8 + (0.9,))
    swapped = DisplaySettings(40, layout=CLASSIC, swap_panels=True)
    data = PanelData(metrics=METRICS, weather=CLEAR_NIGHT)

    left = render_side("left", swapped, snapshot, stereo, 0.2)
    right = render_side("right", swapped, snapshot, stereo, 0.2)

    assert left == render_panel(
        CLASSIC.right, data, spectrum=stereo.left, mirrored=True, weather_frame=0
    ).snapshot()
    assert right == render_panel(CLASSIC.left, data, spectrum=stereo.right).snapshot()


def test_render_side_shows_the_gpu_temperature_where_the_layout_puts_it() -> None:
    layout = Layout(left=(temperature("gpu"),), right=(temperature("cpu"),))
    snapshot = DashboardSnapshot(cpu_temperature_c=92, gpu_temperature_c=63)

    left = render_side("left", DisplaySettings(40, layout=layout), snapshot, None, 0.0)

    assert left == render_panel(layout.left, PanelData(gpu_temperature_c=63)).snapshot()
    assert left != render_panel(layout.left, PanelData(gpu_temperature_c=64)).snapshot()


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (DisplaySettings(40, enabled=False), None),
        (DisplaySettings(40), CaptureRequest("microphone", 1)),
        (DisplaySettings(40, audio_source="system"), CaptureRequest("system", 2)),
        (
            DisplaySettings(
                40, audio_source="system", layout=Layout(left=(equalizer(),), right=(weather(),))
            ),
            CaptureRequest("system", 1),
        ),
        (DisplaySettings(40, layout=Layout(left=(bars(),), right=(weather(),))), None),
    ],
)
def test_wanted_capture_follows_the_panels_the_equalizers_and_the_source(
    settings: DisplaySettings, expected: CaptureRequest | None
) -> None:
    assert wanted_capture(settings) == expected


def test_sensor_sampler_publishes_the_gpu_temperature() -> None:
    state = DashboardState()
    sampler = SensorSampler(
        state,
        read_metrics=lambda: METRICS,
        read_cpu_temperature=lambda: 70.0,
        read_gpu_temperature=lambda: 55.0,
        alert_policy=AlertPolicy(AlertsConfig()),
        interval_seconds=1.0,
        clock=FakeClock(),
    )

    sampler.step()

    assert state.snapshot().gpu_temperature_c == 55.0


def test_sensor_sampler_publishes_metrics_and_alerts_without_touching_weather() -> None:
    state = DashboardState()
    state.update(weather=CLEAR_NIGHT)
    clock = FakeClock()
    rules = {
        ("battery", BlinkRule("below", 10)),
        ("ram", BlinkRule("above", 95)),
        ("cpu", BlinkRule("above", 95)),
    }
    sampler = SensorSampler(
        state,
        read_metrics=lambda: SystemMetrics(battery_percent=8, cpu_percent=35, ram_used_percent=96),
        read_cpu_temperature=lambda: 121.0,
        alert_policy=AlertPolicy(AlertsConfig()),
        bar_rules=lambda: rules,
        interval_seconds=1,
        clock=clock,
    )

    assert sampler.step() == 1
    snapshot = state.snapshot()
    assert snapshot.metrics.battery_percent == 8
    assert snapshot.cpu_temperature_c == 121.0
    assert snapshot.weather == CLEAR_NIGHT
    assert snapshot.alerts == AlertState(cpu_temperature=True)
    assert snapshot.blinking_bars == {
        ("battery", BlinkRule("below", 10)),
        ("ram", BlinkRule("above", 95)),
    }


def test_sensor_sampler_logs_when_a_bar_starts_and_stops_blinking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loads = iter((90.0, 96.0, 94.0, 92.0))
    sampler = SensorSampler(
        DashboardState(),
        read_metrics=lambda: SystemMetrics(battery_percent=80, cpu_percent=next(loads), ram_used_percent=40),
        read_cpu_temperature=lambda: 50.0,
        alert_policy=AlertPolicy(AlertsConfig()),
        bar_rules=lambda: {("cpu", BlinkRule("above", 95))},
        interval_seconds=1,
        clock=FakeClock(),
    )

    with caplog.at_level(logging.INFO):
        for _ in range(4):
            sampler.step()

    assert _messages(caplog) == [
        "CPU ≥ 95 %: bar blinking at 96 %",
        "CPU ≥ 95 %: bar stopped blinking",  # 94 is within the 2 points; 92 is not
    ]


def test_sensor_sampler_logs_when_an_alert_starts_and_stops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    temperatures = iter((100.0, 121.0, 121.5, 114.0))
    sampler = SensorSampler(
        DashboardState(),
        read_metrics=lambda: METRICS,
        read_cpu_temperature=lambda: next(temperatures),
        alert_policy=AlertPolicy(AlertsConfig()),
        interval_seconds=1,
    )

    with caplog.at_level(logging.INFO):
        for _ in range(4):
            sampler.step()

    assert _messages(caplog) == [
        "cpu temperature alert on (121 °C)",
        "cpu temperature alert off (114 °C)",
    ]


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


def test_sound_watch_logs_only_availability_changes(caplog: pytest.LogCaptureFixture) -> None:
    sound = FakeSound()
    watch = SoundWatch(
        sound, wanted=lambda: CaptureRequest(), interval_seconds=1.0, startup_grace_seconds=0
    )

    with caplog.at_level(logging.INFO):
        for current in (None, None, MONO, MONO, None):
            sound.current = current
            assert watch.step() == 1.0

    assert sound.requests == [CaptureRequest()]
    assert _messages(caplog) == [
        "capturing the microphone",
        "sound unavailable, equalizer dark",
        "sound available",
        "sound unavailable, equalizer dark",
    ]


def test_sound_watch_stops_while_nothing_needs_sound_and_follows_the_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sound = FakeSound()
    sound.current = MONO
    wanted: list[CaptureRequest | None] = [CaptureRequest()]
    now = [0.0]
    watch = SoundWatch(
        sound, wanted=lambda: wanted[0], startup_grace_seconds=2.0, clock=lambda: now[0]
    )

    with caplog.at_level(logging.INFO):
        watch.step()  # starts, within its grace: not judged yet
        now[0] = 2.0
        watch.step()
        wanted[0] = None  # panels off, or no equalizer left
        now[0] = 3.0
        watch.step()
        assert sound.stopped is True
        sound.current = None  # stopped: no levels, but no "unavailable" either
        now[0] = 9.0
        watch.step()
        wanted[0] = CaptureRequest("system", 2)
        now[0] = 10.0
        watch.step()  # restarted, within its grace: not judged yet
        now[0] = 12.0
        sound.current = MONO
        watch.step()

    assert sound.requests == [CaptureRequest(), CaptureRequest("system", 2)]
    assert _messages(caplog) == [
        "capturing the microphone",
        "sound available",
        "sound capture stopped",
        "capturing the system sound in stereo",
        "sound available",
    ]


def test_sound_watch_gives_a_slow_capture_its_grace_before_calling_it_dark(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sound = FakeSound()
    now = [0.0]
    watch = SoundWatch(
        sound, wanted=lambda: CaptureRequest(), startup_grace_seconds=2.0, clock=lambda: now[0]
    )

    with caplog.at_level(logging.INFO):
        watch.step()
        now[0] = 1.0
        watch.step()
        now[0] = 2.0
        watch.step()

    assert _messages(caplog) == ["capturing the microphone", "sound unavailable, equalizer dark"]


def test_dashboard_stop_shuts_panels_down_after_their_loops_end() -> None:
    sound = FakeSound()
    stepped = threading.Event()
    events: list[str] = []

    def step() -> float:
        stepped.set()
        return 0.01

    dashboard = Dashboard(
        panels=((Loop("panel-left", step), lambda: events.append("panel shutdown")),),
        background=(Loop("sensors", lambda: 0.01),),
        sound=sound,
        on_stop=(lambda: events.append("control closed"),),
    )
    dashboard.start()
    assert stepped.wait(1.0)
    dashboard.stop()

    assert sound.stopped is True
    assert events == ["panel shutdown", "control closed"]


def test_run_dashboard_stops_cleanly_on_sigterm_and_restores_handlers() -> None:
    events: list[str] = []
    previous_handler = signal.getsignal(signal.SIGTERM)

    class RecordingDashboard:
        restart_requested = False

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


def test_run_dashboard_exits_to_start_again_when_the_dashboard_asks() -> None:
    events: list[str] = []

    class RestartingDashboard:
        restart_requested = False

        def start(self) -> None:
            events.append("start")
            self.restart_requested = True  # as PortWatch does from its thread

        def stop(self) -> None:
            events.append("stop")

    assert run_dashboard(CONFIGURED, dashboard_factory=lambda _config: RestartingDashboard()) == 3
    assert EXIT_PORTS_CHANGED == 3
    assert events == ["start", "stop"]  # stopped as on SIGTERM: the panels are blanked


def test_port_watch_asks_for_a_restart_when_a_port_stays_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    restarts: list[str] = []
    seen = iter([(), ("ttyACM2",), (), ("ttyACM2",), ("ttyACM2",)])
    watch = PortWatch(lambda: restarts.append("restart"), unreachable_ports=lambda: next(seen))

    with caplog.at_level(logging.WARNING):
        delays = [watch.step() for _ in range(4)]
        assert restarts == []  # missing once at a time: a node the kernel is still creating
        watch.step()

    assert delays == [2.0] * 4
    assert restarts == ["restart"]
    assert "LED Matrix port ttyACM2 is not in this container" in caplog.text


def test_dashboard_restart_event_is_what_it_reports() -> None:
    restart = threading.Event()
    dashboard = Dashboard(panels=(), background=(), sound=FakeSound(), restart=restart)

    assert dashboard.restart_requested is False
    restart.set()
    assert dashboard.restart_requested is True


def test_run_dashboard_refuses_to_start_next_to_a_running_dashboard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def dashboard_factory(_config: DashboardConfig) -> None:
        raise ControlError("another dashboard is already listening on /run/user/1000/x.sock")

    with caplog.at_level(logging.ERROR):
        assert run_dashboard(CONFIGURED, dashboard_factory=dashboard_factory) == 1

    assert "already listening" in caplog.text


def test_cli_tray_and_settings_start_the_gui_without_a_config() -> None:
    started: list[str] = []

    def gui_runner(mode: str) -> int:
        started.append(mode)
        return 0

    assert main(["--tray"], gui_runner=gui_runner) == 0
    assert main(["--settings"], gui_runner=gui_runner) == 0
    assert started == ["tray", "settings"]


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
