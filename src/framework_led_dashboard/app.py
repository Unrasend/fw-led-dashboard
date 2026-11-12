"""Long-running dashboard: sensor and weather loops plus one sender thread per panel."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType
from typing import Protocol

from .alerts import AlertInputs, AlertPolicy, AlertState
from .audio import Levels, MicrophoneSpectrum
from .bar_sources import DEFAULT_BARS
from .config import ConfigError, DashboardConfig
from .control import (
    ControlError,
    ControlServer,
    DisplaySettings,
    SettingsStore,
    bars_side,
    default_settings_path,
    default_socket_path,
)
from .device_discovery import resolve_device_path
from .frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from .left_panel import render_left_panel
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
from .right_panel import render_right_panel
from .scheduler import LOGGER, Step, blink_on, run_loop, weather_frame_index
from .system_metrics import SystemMetrics, SystemMetricsProvider
from .thermal import CpuTemperatureProvider
from .weather import OpenMeteoWeatherClient, WeatherProvider, WeatherReading

BLANK_FRAME: FramePixels = tuple(tuple(0 for _ in range(FRAME_WIDTH)) for _ in range(FRAME_HEIGHT))
WEATHER_RETRY_INITIAL_SECONDS = 30.0
WEATHER_STALE_MIN_SECONDS = 2 * 60 * 60
MICROPHONE_WATCH_DELAY_SECONDS = 2.0
JOIN_TIMEOUT_SECONDS = 5.0
DISABLED_POLL_SECONDS = 0.25
SLEEP_REFRESH_SECONDS = 5.0
UNKNOWN_METRICS = SystemMetrics(battery_percent=None, cpu_percent=None, ram_used_percent=None)
NO_ALERTS = AlertState()


class PanelTransport(Protocol):
    def write_frame(self, frame: FramePixels) -> None: ...

    def send(self, command: bytes) -> None: ...

    def close(self) -> None: ...


class Microphone(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def levels(self) -> Levels | None: ...


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    metrics: SystemMetrics = UNKNOWN_METRICS
    cpu_temperature_c: float | None = None
    weather: WeatherReading | None = None
    alerts: AlertState = NO_ALERTS


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

    Which content each one shows is a live setting (DisplaySettings.swap_panels, which starts
    from config.swap_panels), applied by side_renderer.
    """
    left = config.panels.left.identity
    right = config.panels.right.identity
    if left is None or right is None:
        raise ConfigError(
            "configure panels.left.identity and panels.right.identity (see --list-panels)"
        )
    return {"left": left, "right": right}


def side_renderer(
    side: str,
    *,
    settings: Callable[[], DisplaySettings],
    left: Callable[[float], FramePixels],
    right: Callable[[float], FramePixels],
) -> Callable[[float], FramePixels]:
    """Render for the physical panel on `side`: its own content, or the other side's while
    the panels are swapped. The check runs every frame, so a swap shows at once."""
    if side not in ("left", "right"):
        raise ValueError(f"side must be 'left' or 'right', got {side!r}")

    def render(now: float) -> FramePixels:
        shows_bars = (side == "left") != settings().swap_panels
        return left(now) if shows_bars else right(now)

    return render


def render_left(
    snapshot: DashboardSnapshot,
    levels: Levels | None,
    elapsed: float,
    *,
    bars: Sequence[str] = DEFAULT_BARS,
) -> FramePixels:
    return render_left_panel(
        snapshot.metrics,
        bars=bars,
        spectrum=levels,
        alerts=snapshot.alerts,
        blink_on=blink_on(elapsed),
    ).snapshot()


def render_right(snapshot: DashboardSnapshot, levels: Levels | None, elapsed: float) -> FramePixels:
    return render_right_panel(
        cpu_temperature_c=snapshot.cpu_temperature_c,
        weather=snapshot.weather,
        weather_frame=weather_frame_index(elapsed),
        spectrum=levels,
        alerts=snapshot.alerts,
        blink_on=blink_on(elapsed),
    ).snapshot()


class SensorSampler:
    """Read local sensors, evaluate alerts and publish both."""

    def __init__(
        self,
        state: DashboardState,
        *,
        read_metrics: Callable[[], SystemMetrics],
        read_cpu_temperature: Callable[[], float | None],
        alert_policy: AlertPolicy,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state = state
        self._read_metrics = read_metrics
        self._read_cpu_temperature = read_cpu_temperature
        self._alert_policy = alert_policy
        self._interval = interval_seconds
        self._clock = clock

    def step(self) -> float:
        metrics = self._read_metrics()
        cpu_temperature = self._read_cpu_temperature()
        weather = self._state.snapshot().weather
        ram_used = metrics.ram_used_percent
        alerts = self._alert_policy.update(
            AlertInputs(
                battery_percent=metrics.battery_percent,
                cpu_usage_percent=metrics.cpu_percent,
                ram_available_percent=None if ram_used is None else 100 - ram_used,
                cpu_temperature_c=cpu_temperature,
                outside_temperature_c=None if weather is None else weather.temperature_c,
            ),
            now_seconds=self._clock(),
        )
        self._state.update(metrics=metrics, cpu_temperature_c=cpu_temperature, alerts=alerts)
        return self._interval


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
    firmware sleep while off, brightness changes as soon as the settings change."""

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

    def step(self) -> float:
        settings = self._settings()
        try:
            if settings.enabled:
                delay = self._show_frame(settings.brightness_percent)
            else:
                delay = self._keep_asleep()
        except ReconnectPending as pending:
            return pending.retry_after_seconds
        except TransportError as error:
            if self._connected is not False:
                self._logger.warning("%s panel unavailable: %s", self._name, error)
            self._connected = False
            # A fresh connection wakes the panel and sets brightness: redo both from scratch.
            self._brightness = None
            self._slept_at = None
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
        self._transport.write_frame(self._render(started))
        self._slept_at = None
        return max(0.0, self._frame_interval - (self._clock() - started))

    def _keep_asleep(self) -> float:
        # Sleep is idempotent in firmware; repeating it puts back to sleep a panel that woke
        # on its own (e.g. after resume) without the dashboard noticing.
        now = self._clock()
        if self._slept_at is None or now - self._slept_at >= SLEEP_REFRESH_SECONDS:
            self._transport.send(sleep_command(True))
            self._slept_at = now
        return DISABLED_POLL_SECONDS


class MicrophoneWatch:
    def __init__(
        self,
        microphone: Microphone,
        *,
        interval_seconds: float = 1.0,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._microphone = microphone
        self._interval = interval_seconds
        self._logger = logger
        self._available: bool | None = None

    def step(self) -> float:
        available = self._microphone.levels() is not None
        if available != self._available:
            if available:
                self._logger.info("microphone available")
            else:
                self._logger.info("microphone unavailable, equalizer dark")
        self._available = available
        return self._interval


@dataclass(frozen=True, slots=True)
class Loop:
    name: str
    step: Step
    initial_delay_seconds: float = 0.0


class Dashboard:
    """Own the loop threads, the panels' shutdown and the microphone capture."""

    def __init__(
        self,
        *,
        panels: Sequence[tuple[Loop, Callable[[], None]]],
        background: Sequence[Loop],
        microphone: Microphone,
        on_stop: Sequence[Callable[[], None]] = (),
        join_timeout_seconds: float = JOIN_TIMEOUT_SECONDS,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._panels = tuple(panels)
        self._background = tuple(background)
        self._microphone = microphone
        self._on_stop = tuple(on_stop)
        self._join_timeout = join_timeout_seconds
        self._logger = logger
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}

    def start(self) -> None:
        self._microphone.start()
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
        self._microphone.stop()


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
    )
    control = ControlServer(store, path=default_socket_path())
    control.open()
    state = DashboardState()
    microphone = MicrophoneSpectrum()
    started = time.monotonic()
    prepare = prepare_panel(lambda: store.current().brightness_percent)

    def live_left(now: float) -> FramePixels:
        bars = store.current().bars
        return render_left(state.snapshot(), microphone.levels(), now - started, bars=bars)

    def live_right(now: float) -> FramePixels:
        return render_right(state.snapshot(), microphone.levels(), now - started)

    panels = []
    for side in ("left", "right"):
        render = side_renderer(side, settings=store.current, left=live_left, right=live_right)
        transport = MatrixTransport(
            identity=targets[side],
            path_resolver=resolve_device_path,
            on_connect=prepare,
        )
        worker = PanelWorker(
            side,
            transport,
            render,
            settings=store.current,
            max_fps=config.animation_fps,
        )
        panels.append((Loop(f"panel-{side}", worker.step), worker.shutdown))

    sampler = SensorSampler(
        state,
        read_metrics=SystemMetricsProvider().read,
        read_cpu_temperature=CpuTemperatureProvider().read_celsius,
        alert_policy=AlertPolicy(config.alerts),
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
    background = (
        Loop("sensors", sampler.step),
        Loop("weather", weather.step),
        Loop(
            "microphone-watch",
            MicrophoneWatch(microphone).step,
            initial_delay_seconds=MICROPHONE_WATCH_DELAY_SECONDS,
        ),
        Loop("control", control.step),
    )
    settings = store.current()
    LOGGER.info(
        "display: brightness %d%%, %s, bars on the %s panel: %s",
        settings.brightness_percent,
        "on" if settings.enabled else "off",
        bars_side(settings),
        ", ".join(settings.bars),
    )
    return Dashboard(
        panels=panels, background=background, microphone=microphone, on_stop=(control.close,)
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


def run_dashboard(
    config: DashboardConfig,
    *,
    dashboard_factory: Callable[[DashboardConfig], Runnable] = build_dashboard,
    logger: logging.Logger = LOGGER,
) -> int:
    """Run until SIGTERM or SIGINT, then stop the loops and blank the panels."""
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
        while not stop_requested.is_set():
            time.sleep(0.2)
        logger.info("received %s, stopping", signal.Signals(received[0]).name)
    finally:
        dashboard.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    logger.info("dashboard stopped")
    return 0
