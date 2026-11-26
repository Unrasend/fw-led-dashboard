"""Long-running dashboard: sensor and weather loops plus one sender thread per panel."""

from __future__ import annotations

import logging
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from types import FrameType
from typing import Protocol

from .alerts import AlertInputs, AlertPolicy, AlertState
from .audio import CaptureRequest, SoundSpectrum, Spectrum
from .bar_alerts import BarBlinker, BarRule, describe_rule
from .bar_sources import BAR_SOURCES
from .config import ConfigError, DashboardConfig
from .control import (
    ControlError,
    ControlServer,
    DisplaySettings,
    SettingsStore,
    default_settings_path,
    default_socket_path,
    describe_layout,
)
from .device_discovery import resolve_device_path, unreachable_matrix_ports
from .frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from .layout import SIDES, bar_rules
from .location import (
    Coordinates,
    IpLocationClient,
    JsonLocationCache,
    LocationResolver,
    OpenMeteoGeocoder,
    VpnDetector,
    timezone_city,
)
from .matrix_transport import (
    MatrixTransport,
    ReconnectPending,
    TransportError,
    brightness_command,
    prepare_panel,
    sleep_command,
)
from .panel_render import PanelData, render_panel
from .scheduler import LOGGER, Step, blink_on, run_loop, weather_frame_index
from .system_metrics import SystemMetrics, SystemMetricsProvider
from .thermal import CpuTemperatureProvider, GpuTemperatureProvider
from .weather import OpenMeteoWeatherClient, WeatherProvider, WeatherReading

BLANK_FRAME: FramePixels = tuple(tuple(0 for _ in range(FRAME_WIDTH)) for _ in range(FRAME_HEIGHT))
WEATHER_RETRY_INITIAL_SECONDS = 30.0
WEATHER_STALE_MIN_SECONDS = 2 * 60 * 60
SOUND_STARTUP_GRACE_SECONDS = 2.0
JOIN_TIMEOUT_SECONDS = 5.0
DISABLED_POLL_SECONDS = 0.25
FRAME_RESEND_SECONDS = 2.0
SLEEP_REFRESH_SECONDS = 5.0
PORT_WATCH_SECONDS = 2.0
# The Docker wrapper passes the LED Matrix ports in one by one, as they are when the container
# starts, and sets this; the daemon then exits with EXIT_PORTS_CHANGED when a module turns up
# under a port the container lacks, and the wrapper starts it again with the ports as they are.
RESTART_ON_NEW_PORT_ENV = "LED_DASHBOARD_RESTART_ON_NEW_PORT"
EXIT_PORTS_CHANGED = 3
UNKNOWN_METRICS = SystemMetrics(battery_percent=None, cpu_percent=None, ram_used_percent=None)
NO_ALERTS = AlertState()


class PanelTransport(Protocol):
    def write_frame(self, frame: FramePixels) -> None: ...

    def send(self, command: bytes) -> None: ...

    def close(self) -> None: ...


class Sound(Protocol):
    def start(self, request: CaptureRequest) -> None: ...

    def stop(self) -> None: ...

    def levels(self) -> Spectrum | None: ...


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    metrics: SystemMetrics = UNKNOWN_METRICS
    cpu_temperature_c: float | None = None
    gpu_temperature_c: float | None = None
    weather: WeatherReading | None = None
    alerts: AlertState = NO_ALERTS
    blinking_bars: frozenset[BarRule] = frozenset()


class DashboardState:
    """Latest sensor and weather values, shared between the loops."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = DashboardSnapshot()

    def snapshot(self) -> DashboardSnapshot:
        with self._lock:
            return self._snapshot

    def update(self, **changes: object) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)


def panel_targets(config: DashboardConfig) -> dict[str, str]:
    """USB identity of the physical left and right panel.

    Which widgets each one shows is a live setting (DisplaySettings.layout and swap_panels,
    the latter starting from config.swap_panels), applied by render_side on every frame.
    """
    left = config.panels.left.identity
    right = config.panels.right.identity
    if left is None or right is None:
        raise ConfigError(
            "configure panels.left.identity and panels.right.identity (see --list-panels)"
        )
    return {"left": left, "right": right}


def render_side(
    side: str,
    settings: DisplaySettings,
    snapshot: DashboardSnapshot,
    spectrum: Spectrum | None,
    elapsed: float,
) -> FramePixels:
    """The frame for the physical panel on `side`. Its equalizer shows that side's channel and,
    on the left, runs mirrored so the bass of both modules meets in the middle."""
    return render_panel(
        settings.widgets_on(side),
        PanelData(
            metrics=snapshot.metrics,
            cpu_temperature_c=snapshot.cpu_temperature_c,
            gpu_temperature_c=snapshot.gpu_temperature_c,
            weather=snapshot.weather,
            alerts=snapshot.alerts,
            blinking_bars=snapshot.blinking_bars,
        ),
        spectrum=None if spectrum is None else spectrum.side(side),
        mirrored=side == "left",
        weather_frame=weather_frame_index(elapsed),
        blink_on=blink_on(elapsed),
    ).snapshot()


def wanted_capture(settings: DisplaySettings) -> CaptureRequest | None:
    """What to capture for the equalizers: nothing while the panels are off or no panel has
    one; the system sound in stereo only when both panels have one (left and right)."""
    if not settings.enabled:
        return None
    sides = [
        side
        for side in SIDES
        if any(widget.kind == "equalizer" for widget in settings.layout.side(side))
    ]
    if not sides:
        return None
    stereo = settings.audio_source == "system" and len(sides) == 2
    return CaptureRequest(settings.audio_source, 2 if stereo else 1)


class SensorSampler:
    """Read local sensors, evaluate alerts and the bars' blink rules, and publish them."""

    def __init__(
        self,
        state: DashboardState,
        *,
        read_metrics: Callable[[], SystemMetrics],
        read_cpu_temperature: Callable[[], float | None],
        alert_policy: AlertPolicy,
        read_gpu_temperature: Callable[[], float | None] = lambda: None,
        bar_rules: Callable[[], Iterable[BarRule]] = frozenset,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._state = state
        self._read_metrics = read_metrics
        self._read_cpu_temperature = read_cpu_temperature
        self._read_gpu_temperature = read_gpu_temperature
        self._bar_rules = bar_rules
        self._blinker = BarBlinker()
        self._alert_policy = alert_policy
        self._interval = interval_seconds
        self._clock = clock
        self._logger = logger

    def step(self) -> float:
        metrics = self._read_metrics()
        cpu_temperature = self._read_cpu_temperature()
        weather = self._state.snapshot().weather
        alerts = self._alert_policy.update(
            AlertInputs(
                cpu_temperature_c=cpu_temperature,
                outside_temperature_c=None if weather is None else weather.temperature_c,
            ),
            now_seconds=self._clock(),
        )
        previous = self._state.snapshot()
        blinking = self._blinker.update(self._bar_rules(), metrics)
        self._log_changes(previous.alerts, alerts, cpu_temperature)
        self._log_bars(previous.blinking_bars, blinking, metrics)
        self._state.update(
            metrics=metrics,
            cpu_temperature_c=cpu_temperature,
            gpu_temperature_c=self._read_gpu_temperature(),
            alerts=alerts,
            blinking_bars=blinking,
        )
        return self._interval

    def _log_bars(
        self, previous: frozenset[BarRule], current: frozenset[BarRule], metrics: SystemMetrics
    ) -> None:
        for key, rule in sorted(current - previous, key=str):
            value = BAR_SOURCES[key].read(metrics)
            self._logger.info("%s: bar blinking at %.0f %%", describe_rule(key, rule), value or 0)
        for key, rule in sorted(previous - current, key=str):
            self._logger.info("%s: bar stopped blinking", describe_rule(key, rule))

    def _log_changes(
        self, previous: AlertState, current: AlertState, cpu_temperature: float | None
    ) -> None:
        for field in fields(AlertState):
            was, now = getattr(previous, field.name), getattr(current, field.name)
            if was == now:
                continue
            detail = ""
            if field.name == "cpu_temperature" and cpu_temperature is not None:
                detail = f" ({cpu_temperature:.0f} °C)"
            self._logger.info(
                "%s alert %s%s", field.name.replace("_", " "), "on" if now else "off", detail
            )


class WeatherUpdater:
    """Refresh weather by TTL, retry failures sooner, hide readings that grew stale."""

    def __init__(
        self,
        state: DashboardState,
        *,
        resolve_location: Callable[[], Coordinates | None],
        provider: WeatherProvider,
        interval_seconds: float,
        stale_after_seconds: float,
        retry_initial_seconds: float = WEATHER_RETRY_INITIAL_SECONDS,
        wall_clock: Callable[[], float] = time.time,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._state = state
        self._resolve_location = resolve_location
        self._provider = provider
        self._interval = interval_seconds
        self._stale_after = stale_after_seconds
        self._retry_initial = retry_initial_seconds
        self._wall_clock = wall_clock
        self._logger = logger
        self._failures = 0
        self._ever_succeeded = False

    def step(self) -> float:
        previous = self._provider.current()
        location = self._resolve_location()
        cached = previous if location is None else self._provider.refresh(location)
        fresh = cached is not None and cached is not previous

        shown = None
        if cached is not None and self._wall_clock() - cached.saved_at <= self._stale_after:
            shown = cached.reading
        self._state.update(weather=shown)

        if fresh:
            self._report_success(cached.reading, location)
            return self._interval

        delay = min(self._retry_initial * 2**self._failures, self._interval)
        if self._failures == 0:
            reason = "no location" if location is None else "fetch failed"
            self._logger.warning("weather update failed (%s), retrying in %.0f s", reason, delay)
        self._failures += 1
        return delay

    def _report_success(self, reading: WeatherReading, location: Coordinates | None) -> None:
        source = "unknown" if location is None else location.source.value
        summary = (
            f"{reading.temperature_c:.0f}°C, code {reading.weather_code}, "
            f"{'day' if reading.is_day else 'night'} (location from {source})"
        )
        if self._failures:
            self._logger.info("weather recovered: %s", summary)
        elif not self._ever_succeeded:
            self._logger.info("weather: %s", summary)
        else:
            self._logger.debug("weather: %s", summary)
        self._failures = 0
        self._ever_succeeded = True


class PanelWorker:
    """Drive one panel: frames as fast as it accepts them (capped at `max_fps`) while on,
    firmware sleep while off, brightness changes as soon as the settings change.

    A frame identical to the one on the panel is not sent again (each costs 10 USB writes),
    except every FRAME_RESEND_SECONDS: that restores a panel that lost its picture on its own
    and keeps noticing a panel that went away."""

    def __init__(
        self,
        name: str,
        transport: PanelTransport,
        render: Callable[[float], FramePixels],
        *,
        settings: Callable[[], DisplaySettings],
        max_fps: float,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._name = name
        self._transport = transport
        self._render = render
        self._settings = settings
        self._frame_interval = 1 / max_fps
        self._clock = clock
        self._logger = logger
        self._connected: bool | None = None
        self._brightness: int | None = None
        self._slept_at: float | None = None
        self._shown: FramePixels | None = None
        self._shown_at = -math.inf

    def step(self) -> float:
        settings = self._settings()
        try:
            if settings.enabled:
                delay = self._show_frame(settings.brightness_percent)
            else:
                delay = self._keep_asleep()
        except ReconnectPending as pending:
            self._shown = None
            return pending.retry_after_seconds
        except TransportError as error:
            if self._connected is not False:
                self._logger.warning("%s panel unavailable: %s", self._name, error)
            self._connected = False
            # A fresh connection wakes the panel and sets brightness: redo both from scratch.
            self._brightness = None
            self._slept_at = None
            self._shown = None
            return 0.0

        if self._connected is not True:
            self._logger.info("%s panel connected", self._name)
        self._connected = True
        return delay

    def shutdown(self) -> None:
        """Blank a lit panel and close the port; a panel that is off stays asleep."""
        if self._settings().enabled:
            try:
                self._transport.write_frame(BLANK_FRAME)
            except (TransportError, ReconnectPending):
                pass
        self._transport.close()

    def _show_frame(self, brightness_percent: int) -> float:
        started = self._clock()
        if brightness_percent != self._brightness:
            self._transport.send(brightness_command(brightness_percent))
            self._brightness = brightness_percent
        frame = self._render(started)
        if frame != self._shown or started - self._shown_at >= FRAME_RESEND_SECONDS:
            self._transport.write_frame(frame)
            self._shown, self._shown_at = frame, started
        self._slept_at = None
        return max(0.0, self._frame_interval - (self._clock() - started))

    def _keep_asleep(self) -> float:
        # Sleep is idempotent in firmware; repeating it puts back to sleep a panel that woke
        # on its own (e.g. after resume) without the dashboard noticing.
        now = self._clock()
        self._shown = None  # whatever it showed is gone once it sleeps
        if self._slept_at is None or now - self._slept_at >= SLEEP_REFRESH_SECONDS:
            self._transport.send(sleep_command(True))
            self._slept_at = now
        return DISABLED_POLL_SECONDS


class SoundWatch:
    """Keep the sound capture in line with the settings, and log when it comes and goes.

    Nothing is captured while the panels are off or no panel has an equalizer: that saves the
    spectrum analysis and frees the microphone (desktops show it as in use while parec or
    pw-record runs). A capture that just (re)started gets `startup_grace_seconds` before it
    counts as unavailable.
    """

    def __init__(
        self,
        sound: Sound,
        *,
        wanted: Callable[[], CaptureRequest | None],
        interval_seconds: float = 1.0,
        startup_grace_seconds: float = SOUND_STARTUP_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._sound = sound
        self._wanted = wanted
        self._interval = interval_seconds
        self._grace = startup_grace_seconds
        self._clock = clock
        self._logger = logger
        self._capturing: CaptureRequest | None = None
        self._judge_after = -math.inf
        self._available: bool | None = None

    def step(self) -> float:
        wanted = self._wanted()
        if wanted != self._capturing:
            self._capturing = wanted
            self._available = None
            if wanted is None:
                self._sound.stop()
                self._logger.info("sound capture stopped")
            else:
                self._sound.start(wanted)
                self._judge_after = self._clock() + self._grace
                self._logger.info("capturing %s", _describe_capture(wanted))
        if self._capturing is None or self._clock() < self._judge_after:
            return self._interval
        available = self._sound.levels() is not None
        if available != self._available:
            if available:
                self._logger.info("sound available")
            else:
                self._logger.info("sound unavailable, equalizer dark")
        self._available = available
        return self._interval


def _describe_capture(request: CaptureRequest) -> str:
    what = "the microphone" if request.source == "microphone" else "the system sound"
    return f"{what} in stereo" if request.channels == 2 else what


class PortWatch:
    """Ask for a restart when an LED Matrix port has no node in this /dev (see
    RESTART_ON_NEW_PORT_ENV). Only a port missing on two checks in a row counts: the kernel
    lists a new port a moment before its node appears."""

    def __init__(
        self,
        request_restart: Callable[[], None],
        *,
        unreachable_ports: Callable[[], Sequence[str]] = unreachable_matrix_ports,
        interval_seconds: float = PORT_WATCH_SECONDS,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._request_restart = request_restart
        self._unreachable_ports = unreachable_ports
        self._interval = interval_seconds
        self._logger = logger
        self._seen: tuple[str, ...] = ()

    def step(self) -> float:
        unreachable = tuple(self._unreachable_ports())
        if unreachable and unreachable == self._seen:
            self._logger.warning(
                "LED Matrix port %s is not in this container: exiting so it starts again with it",
                ", ".join(unreachable),
            )
            self._request_restart()
        self._seen = unreachable
        return self._interval


@dataclass(frozen=True, slots=True)
class Loop:
    name: str
    step: Step
    initial_delay_seconds: float = 0.0


class Dashboard:
    """Own the loop threads, the panels' shutdown and the sound capture."""

    def __init__(
        self,
        *,
        panels: Sequence[tuple[Loop, Callable[[], None]]],
        background: Sequence[Loop],
        sound: Sound,
        on_stop: Sequence[Callable[[], None]] = (),
        restart: threading.Event | None = None,
        join_timeout_seconds: float = JOIN_TIMEOUT_SECONDS,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._panels = tuple(panels)
        self._background = tuple(background)
        self._sound = sound
        self._on_stop = tuple(on_stop)
        self._restart = restart or threading.Event()
        self._join_timeout = join_timeout_seconds
        self._logger = logger
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}

    @property
    def restart_requested(self) -> bool:
        """A loop asked for the process to exit and start again (see PortWatch)."""
        return self._restart.is_set()

    def start(self) -> None:
        for loop in (*(loop for loop, _shutdown in self._panels), *self._background):
            thread = threading.Thread(
                target=run_loop,
                args=(loop.step, self._stop),
                kwargs={"name": loop.name, "initial_delay_seconds": loop.initial_delay_seconds},
                name=loop.name,
                daemon=True,
            )
            self._threads[loop.name] = thread
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        deadline = time.monotonic() + self._join_timeout
        for thread in self._threads.values():
            thread.join(max(0.0, deadline - time.monotonic()))

        for loop, shutdown in self._panels:
            thread = self._threads.get(loop.name)
            if thread is not None and thread.is_alive():
                self._logger.warning("%s did not stop in time; leaving its panel as is", loop.name)
                continue
            shutdown()
        for callback in self._on_stop:
            callback()
        self._sound.stop()


def default_cache_directory() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", "")
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".cache"
    return root / "framework-led-dashboard"


def build_dashboard(config: DashboardConfig) -> Dashboard:
    """Wire the real providers; raises ControlError if another dashboard already runs."""
    targets = panel_targets(config)
    store = SettingsStore.load(
        default_settings_path(),
        default_brightness_percent=config.brightness_percent,
        default_swap_panels=config.swap_panels,
        default_cpu_alert_celsius=config.alerts.cpu_temperature_critical_c,
    )
    state = DashboardState()
    sound = SoundSpectrum()
    started = time.monotonic()

    def render(side: str, now: float) -> FramePixels:
        return render_side(side, store.current(), state.snapshot(), sound.levels(), now - started)

    def current_frames() -> dict[str, FramePixels]:
        now = time.monotonic()
        return {side: render(side, now) for side in SIDES}

    control = ControlServer(store, path=default_socket_path(), frames=current_frames)
    control.open()
    prepare = prepare_panel(lambda: store.current().brightness_percent)

    panels = []
    for side in SIDES:
        transport = MatrixTransport(
            identity=targets[side],
            path_resolver=resolve_device_path,
            on_connect=prepare,
        )
        worker = PanelWorker(
            side,
            transport,
            lambda now, side=side: render(side, now),
            settings=store.current,
            max_fps=config.animation_fps,
        )
        panels.append((Loop(f"panel-{side}", worker.step), worker.shutdown))

    sampler = SensorSampler(
        state,
        read_metrics=SystemMetricsProvider().read,
        read_cpu_temperature=CpuTemperatureProvider().read_celsius,
        read_gpu_temperature=GpuTemperatureProvider().read_celsius,
        bar_rules=lambda: bar_rules(store.current().layout),
        alert_policy=AlertPolicy(
            config.alerts,
            cpu_temperature_critical=lambda: store.current().cpu_alert_celsius,
        ),
        interval_seconds=config.updates.metrics_seconds,
    )
    weather_seconds = config.updates.weather_seconds
    weather = WeatherUpdater(
        state,
        resolve_location=_location_resolver(config).resolve,
        provider=WeatherProvider(fetch_weather=OpenMeteoWeatherClient().fetch),
        interval_seconds=weather_seconds,
        stale_after_seconds=max(WEATHER_STALE_MIN_SECONDS, 3 * weather_seconds),
    )
    restart = threading.Event()
    background = [
        Loop("sensors", sampler.step),
        Loop("weather", weather.step),
        Loop("sound-watch", SoundWatch(sound, wanted=lambda: wanted_capture(store.current())).step),
        Loop("control", control.step),
    ]
    if os.environ.get(RESTART_ON_NEW_PORT_ENV) == "1":
        background.append(Loop("port-watch", PortWatch(restart.set).step))
    settings = store.current()
    LOGGER.info(
        "display: brightness %d%%, %s%s; layout: %s; equalizer source: %s",
        settings.brightness_percent,
        "on" if settings.enabled else "off",
        ", panels swapped" if settings.swap_panels else "",
        describe_layout(settings.layout),
        settings.audio_source,
    )
    return Dashboard(
        panels=panels, background=background, sound=sound, on_stop=(control.close,), restart=restart
    )


def _location_resolver(config: DashboardConfig) -> LocationResolver:
    cache = JsonLocationCache(default_cache_directory() / "location.json")
    geocoder = OpenMeteoGeocoder()
    return LocationResolver(
        config.location,
        vpn_status=VpnDetector(interface_prefixes=config.location.vpn_interface_prefixes).status,
        ip_lookup=IpLocationClient().lookup,
        load_cache=cache.load,
        save_cache=cache.save,
        timezone_lookup=lambda: geocoder.lookup_city(timezone_city()),
    )


class Runnable(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    @property
    def restart_requested(self) -> bool: ...


def run_dashboard(
    config: DashboardConfig,
    *,
    dashboard_factory: Callable[[DashboardConfig], Runnable] = build_dashboard,
    logger: logging.Logger = LOGGER,
) -> int:
    """Run until SIGTERM or SIGINT, then stop the loops and blank the panels.

    Returns EXIT_PORTS_CHANGED instead of 0 when the dashboard asked to be started again."""
    stop_requested = threading.Event()

    received: list[int] = []

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        # Only records and sets a flag: the main thread never holds the event's lock while it
        # polls below, and logging waits until the handler has returned.
        received.append(signum)
        stop_requested.set()

    try:
        dashboard = dashboard_factory(config)
    except ControlError as error:
        logger.error("cannot start: %s", error)
        return 1

    handled = (signal.SIGTERM, signal.SIGINT)
    previous = {signum: signal.signal(signum, request_stop) for signum in handled}
    try:
        dashboard.start()
        logger.info("dashboard running")
        while not stop_requested.is_set() and not dashboard.restart_requested:
            time.sleep(0.2)
        if received:
            logger.info("received %s, stopping", signal.Signals(received[0]).name)
    finally:
        dashboard.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if not received:
        logger.info("dashboard stopped to start again")
        return EXIT_PORTS_CHANGED
    logger.info("dashboard stopped")
    return 0
