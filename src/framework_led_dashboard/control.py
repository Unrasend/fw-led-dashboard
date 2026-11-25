"""Runtime display settings and the local control socket the tray talks to.

Protocol: one JSON object per line, one request per connection.
  {"command": "status"}
  {"command": "set", "brightness_percent": 60, "enabled": false}   (every field optional)
Settable fields: brightness_percent, enabled, bars (3 source names), swap_panels,
cpu_alert_celsius.
Answer: {"ok": true, <every field>} or {"ok": false, "error": "..."}.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .bar_sources import DEFAULT_BARS, validate_bars
from .config import CPU_ALERT_MAX_CELSIUS, CPU_ALERT_MIN_CELSIUS
from .scheduler import LOGGER

MAX_MESSAGE_BYTES = 4096
SOCKET_NAME = "framework-led-dashboard.sock"
CONNECTION_TIMEOUT_SECONDS = 1.0


SETTABLE_FIELDS = frozenset(
    {"brightness_percent", "enabled", "bars", "swap_panels", "cpu_alert_celsius"}
)
DEFAULT_CPU_ALERT_CELSIUS = 120


class ControlError(OSError):
    """Raised when the dashboard control socket cannot be used."""


@dataclass(frozen=True, slots=True)
class DisplaySettings:
    brightness_percent: int
    enabled: bool = True
    bars: tuple[str, ...] = DEFAULT_BARS
    swap_panels: bool = False  # bars on the right panel, weather on the left
    cpu_alert_celsius: float = DEFAULT_CPU_ALERT_CELSIUS  # the CPU temperature blinks from here

    def __post_init__(self) -> None:
        brightness = self.brightness_percent
        if isinstance(brightness, bool) or not isinstance(brightness, int):
            raise TypeError("brightness_percent must be an integer between 0 and 100")
        if not 0 <= brightness <= 100:
            raise ValueError("brightness_percent must be an integer between 0 and 100")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be true or false")
        if not isinstance(self.bars, tuple) or not all(isinstance(key, str) for key in self.bars):
            raise TypeError("bars must be a tuple of source names")
        validate_bars(self.bars)
        if not isinstance(self.swap_panels, bool):
            raise TypeError("swap_panels must be true or false")
        alert = self.cpu_alert_celsius
        if isinstance(alert, bool) or not isinstance(alert, (int, float)):
            raise TypeError("cpu_alert_celsius must be a number")
        if not CPU_ALERT_MIN_CELSIUS <= alert <= CPU_ALERT_MAX_CELSIUS:  # also rejects NaN
            raise ValueError(
                f"cpu_alert_celsius must be between {CPU_ALERT_MIN_CELSIUS} and "
                f"{CPU_ALERT_MAX_CELSIUS}"
            )


def bars_side(settings: DisplaySettings) -> str:
    """The panel that shows the bars: "left", or "right" when the panels are swapped."""
    return "right" if settings.swap_panels else "left"


def weather_side(settings: DisplaySettings) -> str:
    """The panel that shows the weather and the CPU temperature."""
    return "left" if settings.swap_panels else "right"


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime and Path(runtime).is_absolute():
        return Path(runtime) / SOCKET_NAME
    return Path(f"/run/user/{os.getuid()}") / SOCKET_NAME


def default_settings_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME", "")
    base = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local" / "state"
    return base / "framework-led-dashboard" / "display.json"


class SettingsStore:
    """Thread-safe display settings, persisted so a restart keeps the tray's choice."""

    def __init__(
        self,
        initial: DisplaySettings,
        *,
        path: Path | None = None,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._lock = threading.Lock()
        self._settings = initial
        self._path = path
        self._logger = logger

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        default_brightness_percent: int,
        default_swap_panels: bool = False,
        default_cpu_alert_celsius: float = DEFAULT_CPU_ALERT_CELSIUS,
        logger: logging.Logger = LOGGER,
    ) -> SettingsStore:
        settings = DisplaySettings(
            default_brightness_percent,
            swap_panels=default_swap_panels,
            cpu_alert_celsius=default_cpu_alert_celsius,
        )
        try:
            body = path.read_bytes()
            if len(body) > MAX_MESSAGE_BYTES:
                raise ValueError("file is too large")
            raw = json.loads(body)
            if not isinstance(raw, dict):
                raise TypeError("expected a JSON object")
            settings = DisplaySettings(
                raw["brightness_percent"],
                raw["enabled"],
                _bars_from_json(raw.get("bars")),
                # Fields saved by an older version are missing: the config still decides.
                raw.get("swap_panels", default_swap_panels),
                raw.get("cpu_alert_celsius", default_cpu_alert_celsius),
            )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as error:
            logger.warning("ignoring display settings in %s: %s", path, error)
        return cls(settings, path=path, logger=logger)

    def current(self) -> DisplaySettings:
        with self._lock:
            return self._settings

    def update(
        self,
        *,
        brightness_percent: int | None = None,
        enabled: bool | None = None,
        bars: tuple[str, ...] | None = None,
        swap_panels: bool | None = None,
        cpu_alert_celsius: float | None = None,
    ) -> DisplaySettings:
        changes: dict[str, object] = {}
        if brightness_percent is not None:
            changes["brightness_percent"] = brightness_percent
        if enabled is not None:
            changes["enabled"] = enabled
        if bars is not None:
            changes["bars"] = bars
        if swap_panels is not None:
            changes["swap_panels"] = swap_panels
        if cpu_alert_celsius is not None:
            changes["cpu_alert_celsius"] = cpu_alert_celsius
        with self._lock:
            previous = self._settings
            updated = replace(previous, **changes)
            if updated == previous:
                return updated
            self._settings = updated
            self._save(updated)
        if updated.enabled != previous.enabled:
            self._logger.info("display turned %s", "on" if updated.enabled else "off")
        if updated.brightness_percent != previous.brightness_percent:
            self._logger.debug("display brightness %d%%", updated.brightness_percent)
        if updated.bars != previous.bars:
            self._logger.info("bars: %s", ", ".join(updated.bars))
        if updated.swap_panels != previous.swap_panels:
            self._logger.info("bars on the %s panel", bars_side(updated))
        if updated.cpu_alert_celsius != previous.cpu_alert_celsius:
            self._logger.info("CPU temperature alert at %g °C", updated.cpu_alert_celsius)
        return updated

    def _save(self, settings: DisplaySettings) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
            temporary.write_text(json.dumps(asdict(settings)), encoding="utf-8")
            temporary.replace(self._path)
        except OSError as error:
            self._logger.warning("could not save display settings to %s: %s", self._path, error)


def handle_request(store: SettingsStore, request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        return _error("request must be a JSON object")
    command = request.get("command")
    try:
        if command == "status":
            settings = store.current()
        elif command == "set":
            unknown = set(request) - {"command", *SETTABLE_FIELDS}
            if unknown:
                return _error(f"unknown fields: {', '.join(sorted(unknown))}")
            bars = request.get("bars")
            settings = store.update(
                brightness_percent=request.get("brightness_percent"),
                enabled=request.get("enabled"),
                bars=None if bars is None else _bars_from_json(bars),
                swap_panels=request.get("swap_panels"),
                cpu_alert_celsius=request.get("cpu_alert_celsius"),
            )
        else:
            return _error("command must be 'status' or 'set'")
    except (TypeError, ValueError) as error:
        return _error(str(error))
    return {"ok": True, **asdict(settings)}


class ControlServer:
    """Answer tray requests on a Unix socket that only the current user can open."""

    def __init__(
        self,
        store: SettingsStore,
        *,
        path: Path,
        accept_timeout_seconds: float = 0.5,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._store = store
        self._path = path
        self._accept_timeout = accept_timeout_seconds
        self._logger = logger
        self._socket: socket.socket | None = None

    def open(self) -> None:
        if _socket_answers(self._path):
            raise ControlError(f"another dashboard is already listening on {self._path}")
        self._path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self._path))
            os.chmod(self._path, 0o600)
            listener.listen(4)
            listener.settimeout(self._accept_timeout)
        except OSError:
            listener.close()
            raise
        self._socket = listener

    def step(self) -> float:
        if self._socket is None:
            raise ControlError("control server is not open")
        try:
            connection, _address = self._socket.accept()
        except TimeoutError:
            return 0.0
        with connection:
            connection.settimeout(CONNECTION_TIMEOUT_SECONDS)
            try:
                response = handle_request(self._store, _read_message(connection))
            except ValueError as error:
                response = _error(str(error))
            except OSError as error:
                self._logger.debug("control client went away: %s", error)
                return 0.0
            try:
                connection.sendall(_encode(response))
            except OSError as error:
                self._logger.debug("control client went away: %s", error)
        return 0.0

    def close(self) -> None:
        listener = self._socket
        self._socket = None
        if listener is None:
            return
        listener.close()
        self._path.unlink(missing_ok=True)


class ControlClient:
    def __init__(
        self,
        path: Path | None = None,
        *,
        timeout_seconds: float = CONNECTION_TIMEOUT_SECONDS,
    ) -> None:
        self._path = path or default_socket_path()
        self._timeout = timeout_seconds

    def status(self) -> DisplaySettings:
        return self._request({"command": "status"})

    def set(
        self,
        *,
        brightness_percent: int | None = None,
        enabled: bool | None = None,
        bars: Sequence[str] | None = None,
        swap_panels: bool | None = None,
        cpu_alert_celsius: float | None = None,
    ) -> DisplaySettings:
        request: dict[str, object] = {"command": "set"}
        if brightness_percent is not None:
            request["brightness_percent"] = brightness_percent
        if enabled is not None:
            request["enabled"] = enabled
        if bars is not None:
            request["bars"] = list(bars)
        if swap_panels is not None:
            request["swap_panels"] = swap_panels
        if cpu_alert_celsius is not None:
            request["cpu_alert_celsius"] = cpu_alert_celsius
        return self._request(request)

    def _request(self, request: dict[str, object]) -> DisplaySettings:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._timeout)
                connection.connect(str(self._path))
                connection.sendall(_encode(request))
                response = _read_message(connection)
        except OSError as error:
            raise ControlError(f"dashboard is not reachable at {self._path}: {error}") from error
        except ValueError as error:
            raise ControlError(f"invalid answer from dashboard: {error}") from error

        if not isinstance(response, dict):
            raise ControlError("invalid answer from dashboard: expected a JSON object")
        if response.get("ok") is not True:
            raise ControlError(str(response.get("error", "request failed")))
        try:
            return DisplaySettings(
                response["brightness_percent"],
                response["enabled"],
                _bars_from_json(response["bars"]),
                response["swap_panels"],
                response["cpu_alert_celsius"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ControlError(f"invalid answer from dashboard: {error}") from error


def _bars_from_json(value: object) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_BARS
    if not isinstance(value, list):
        raise TypeError("bars must be a list of 3 source names")
    return tuple(value)


def _socket_answers(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(CONNECTION_TIMEOUT_SECONDS)
        try:
            probe.connect(str(path))
        except OSError:
            return False
    return True


def _read_message(connection: socket.socket) -> object:
    data = b""
    while b"\n" not in data:
        chunk = connection.recv(MAX_MESSAGE_BYTES)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("message is too large")
    try:
        return json.loads(data.split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("message is not valid JSON") from error


def _encode(message: dict[str, object]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def _error(message: str) -> dict[str, object]:
    return {"ok": False, "error": message}
