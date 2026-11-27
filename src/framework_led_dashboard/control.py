"""Runtime display settings and the local control socket the tray talks to.

Protocol: one JSON object per line, one request per connection.
  {"command": "status"}
  {"command": "set", "brightness_percent": 60, "enabled": false}   (every field optional)
  {"command": "frames"}
Settable fields: brightness_percent, enabled, swap_panels, cpu_alert_celsius, layout (see
layout.py), audio_source ("microphone" or "system").
Answer: {"ok": true, <every field>, "panels": ["left", "right"]} or {"ok": false, "error":
"..."}; "panels" names the modules the dashboard drives, one of them on a laptop with a single
module. For "frames", {"ok": true, "left": "<hex>", "right": "<hex>"}: what each of those
modules shows now, 34 rows of 9 pixels, one byte per pixel, rendered even while they are off.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .audio import AUDIO_SOURCES
from .config import CPU_ALERT_MAX_CELSIUS, CPU_ALERT_MIN_CELSIUS
from .frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from .layout import DEFAULT_LAYOUT, SIDES, Layout, Widget, default_layout, layout_from_json
from .scheduler import LOGGER

MAX_MESSAGE_BYTES = 16384
# The socket sits in a folder of its own, so a container can be given just that folder.
SOCKET_DIRECTORY = "framework-led-dashboard"
SOCKET_NAME = "control.sock"
CONNECTION_TIMEOUT_SECONDS = 1.0


SETTABLE_FIELDS = frozenset(
    {"brightness_percent", "enabled", "swap_panels", "cpu_alert_celsius", "layout", "audio_source"}
)
DEFAULT_CPU_ALERT_CELSIUS = 120


class ControlError(OSError):
    """Raised when the dashboard control socket cannot be used."""


@dataclass(frozen=True, slots=True)
class DisplaySettings:
    brightness_percent: int
    enabled: bool = True
    swap_panels: bool = False  # each panel shows the other one's widgets
    cpu_alert_celsius: float = DEFAULT_CPU_ALERT_CELSIUS  # the CPU temperature blinks from here
    layout: Layout = DEFAULT_LAYOUT
    audio_source: str = "microphone"  # what the equalizer shows: microphone or system sound

    def __post_init__(self) -> None:
        brightness = self.brightness_percent
        if isinstance(brightness, bool) or not isinstance(brightness, int):
            raise TypeError("brightness_percent must be an integer between 0 and 100")
        if not 0 <= brightness <= 100:
            raise ValueError("brightness_percent must be an integer between 0 and 100")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be true or false")
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
        if not isinstance(self.layout, Layout):
            raise TypeError("layout must be a Layout")
        if self.audio_source not in AUDIO_SOURCES:
            raise ValueError(f"audio_source must be one of {', '.join(AUDIO_SOURCES)}")

    def widgets_on(self, side: str) -> tuple[Widget, ...]:
        """The widgets the physical panel on `side` shows (the other side's when swapped)."""
        if self.swap_panels:
            side = "right" if side == "left" else "left"
        return self.layout.side(side)

    def to_json(self) -> dict[str, object]:
        return {
            "brightness_percent": self.brightness_percent,
            "enabled": self.enabled,
            "swap_panels": self.swap_panels,
            "cpu_alert_celsius": self.cpu_alert_celsius,
            "layout": self.layout.to_json(),
            "audio_source": self.audio_source,
        }


def settings_from_json(raw: Mapping[str, object], defaults: DisplaySettings) -> DisplaySettings:
    """Settings from JSON; a missing field keeps its default. A file saved before layouts
    existed has "bars" instead: they go into the default layout."""
    layout = defaults.layout
    if "layout" in raw:
        layout = layout_from_json(raw["layout"])
    elif "bars" in raw:
        bars = raw["bars"]
        if not isinstance(bars, list):
            raise TypeError("bars must be a list of 3 source names")
        layout = default_layout(bars)
    return DisplaySettings(
        raw.get("brightness_percent", defaults.brightness_percent),  # type: ignore[arg-type]
        enabled=raw.get("enabled", defaults.enabled),  # type: ignore[arg-type]
        swap_panels=raw.get("swap_panels", defaults.swap_panels),  # type: ignore[arg-type]
        cpu_alert_celsius=raw.get("cpu_alert_celsius", defaults.cpu_alert_celsius),  # type: ignore[arg-type]
        layout=layout,
        audio_source=raw.get("audio_source", defaults.audio_source),  # type: ignore[arg-type]
    )


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    base = Path(runtime) if runtime and Path(runtime).is_absolute() else Path(f"/run/user/{os.getuid()}")
    return base / SOCKET_DIRECTORY / SOCKET_NAME


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
            missing = {"brightness_percent", "enabled"} - set(raw)
            if missing:
                raise KeyError(", ".join(sorted(missing)))
            # Fields saved by an older version are missing: the config still decides.
            settings = settings_from_json(raw, settings)
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
        swap_panels: bool | None = None,
        cpu_alert_celsius: float | None = None,
        layout: Layout | None = None,
        audio_source: str | None = None,
    ) -> DisplaySettings:
        given = {
            "brightness_percent": brightness_percent,
            "enabled": enabled,
            "swap_panels": swap_panels,
            "cpu_alert_celsius": cpu_alert_celsius,
            "layout": layout,
            "audio_source": audio_source,
        }
        changes = {name: value for name, value in given.items() if value is not None}
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
        if updated.swap_panels != previous.swap_panels:
            self._logger.info("panels %s", "swapped" if updated.swap_panels else "not swapped")
        if updated.cpu_alert_celsius != previous.cpu_alert_celsius:
            self._logger.info("CPU temperature alert at %g °C", updated.cpu_alert_celsius)
        if updated.layout != previous.layout:
            self._logger.info("layout: %s", describe_layout(updated.layout))
        if updated.audio_source != previous.audio_source:
            self._logger.info("equalizer source: %s", updated.audio_source)
        return updated

    def _save(self, settings: DisplaySettings) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
            temporary.write_text(json.dumps(settings.to_json()), encoding="utf-8")
            temporary.replace(self._path)
        except OSError as error:
            self._logger.warning("could not save display settings to %s: %s", self._path, error)


def describe_layout(layout: Layout) -> str:
    return "; ".join(
        f"{side} " + ", ".join(widget.label.lower() for widget in layout.side(side))
        for side in ("left", "right")
    )


FrameSource = Callable[[], Mapping[str, FramePixels]]


def handle_request(
    store: SettingsStore,
    request: object,
    *,
    frames: FrameSource | None = None,
    panels: Sequence[str] = SIDES,
) -> dict[str, object]:
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
            layout = request.get("layout")
            settings = store.update(
                brightness_percent=request.get("brightness_percent"),
                enabled=request.get("enabled"),
                swap_panels=request.get("swap_panels"),
                cpu_alert_celsius=request.get("cpu_alert_celsius"),
                layout=None if layout is None else layout_from_json(layout),
                audio_source=request.get("audio_source"),
            )
        elif command == "frames" and frames is not None:
            shown = frames()
            return {"ok": True, **{side: encode_frame(shown[side]) for side in panels}}
        else:
            return _error("command must be 'status', 'set' or 'frames'")
    except (TypeError, ValueError) as error:
        return _error(str(error))
    return {"ok": True, **settings.to_json(), "panels": list(panels)}


def encode_frame(frame: FramePixels) -> str:
    return bytes(value for row in frame for value in row).hex()


def decode_frame(text: str) -> FramePixels:
    data = bytes.fromhex(text)
    if len(data) != FRAME_WIDTH * FRAME_HEIGHT:
        raise ValueError(f"a frame has {FRAME_WIDTH * FRAME_HEIGHT} pixels, got {len(data)}")
    return tuple(
        tuple(data[row * FRAME_WIDTH : (row + 1) * FRAME_WIDTH]) for row in range(FRAME_HEIGHT)
    )


class ControlServer:
    """Answer tray requests on a Unix socket that only the current user can open."""

    def __init__(
        self,
        store: SettingsStore,
        *,
        path: Path,
        frames: FrameSource | None = None,
        panels: Sequence[str] = SIDES,
        accept_timeout_seconds: float = 0.5,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._store = store
        self._frames = frames
        self._panels = tuple(panels)
        self._path = path
        self._accept_timeout = accept_timeout_seconds
        self._logger = logger
        self._socket: socket.socket | None = None

    def open(self) -> None:
        if _socket_answers(self._path):
            raise ControlError(f"another dashboard is already listening on {self._path}")
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
                response = handle_request(
                    self._store,
                    _read_message(connection),
                    frames=self._frames,
                    panels=self._panels,
                )
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
        # The modules the dashboard drives, as its last answer to status() or set() named them.
        self.panels: tuple[str, ...] = SIDES

    def status(self) -> DisplaySettings:
        return self._settings(self._request({"command": "status"}))

    def set(
        self,
        *,
        brightness_percent: int | None = None,
        enabled: bool | None = None,
        swap_panels: bool | None = None,
        cpu_alert_celsius: float | None = None,
        layout: Layout | None = None,
        audio_source: str | None = None,
    ) -> DisplaySettings:
        request: dict[str, object] = {"command": "set"}
        if brightness_percent is not None:
            request["brightness_percent"] = brightness_percent
        if enabled is not None:
            request["enabled"] = enabled
        if swap_panels is not None:
            request["swap_panels"] = swap_panels
        if cpu_alert_celsius is not None:
            request["cpu_alert_celsius"] = cpu_alert_celsius
        if layout is not None:
            request["layout"] = layout.to_json()
        if audio_source is not None:
            request["audio_source"] = audio_source
        return self._settings(self._request(request))

    def frames(self) -> dict[str, FramePixels]:
        """What each module the dashboard drives shows now: the left, the right or both."""
        response = self._request({"command": "frames"})
        try:
            frames = {side: decode_frame(response[side]) for side in SIDES if side in response}  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ControlError(f"invalid answer from dashboard: {error}") from error
        if not frames:
            raise ControlError("invalid answer from dashboard: no frames")
        return frames

    def _request(self, request: dict[str, object]) -> dict[str, object]:
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
        return response

    def _settings(self, response: dict[str, object]) -> DisplaySettings:
        try:
            settings = settings_from_json(response, DisplaySettings(0))
            self.panels = _panels(response.get("panels", list(SIDES)))  # 1.6 and older: no "panels"
        except (KeyError, TypeError, ValueError) as error:
            raise ControlError(f"invalid answer from dashboard: {error}") from error
        return settings


def _panels(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or any(side not in SIDES for side in raw):
        raise ValueError("panels must name the left module, the right one or both")
    return tuple(side for side in SIDES if side in raw)


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
