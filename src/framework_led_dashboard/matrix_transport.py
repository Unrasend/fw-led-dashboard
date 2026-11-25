from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar

from .device_discovery import DeviceDiscoveryError
from .frame import FRAME_HEIGHT, FRAME_WIDTH
from .serial_port import SerialPort

_FRAMEWORK_MAGIC = bytes([0x32, 0xAC])
_BRIGHTNESS = 0x00
_SLEEP = 0x03
_ANIMATE = 0x04
_STAGE_GREYSCALE_COLUMN = 0x07
_DRAW_GREYSCALE_BUFFER = 0x08
_RESPONSE_SIZE = 32
# The firmware fades a waking panel in with `old_brightness - 5` on a u8. A stored level below 5
# wraps to ~250, so the panel flares to full brightness and then drops back: never go below 5.
MIN_BRIGHTNESS_LEVEL = 5
# A waking panel ignores USB during its fade-in: up to 255 / 5 steps x 100 ms = 5.1 s.
# Both timeouts cover it, so a wake-up (e.g. after suspend) stalls a frame instead of failing it.
_SERIAL_READ_TIMEOUT_SECONDS = 6.0
_SERIAL_WRITE_TIMEOUT_SECONDS = 6.0


class TransportError(RuntimeError):
    """Raised after a serial or discovery operation fails."""


class ReconnectPending(RuntimeError):
    """Raised when reconnect backoff has not elapsed yet."""

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"reconnect allowed in {retry_after_seconds:.3f} seconds")
        self.retry_after_seconds = retry_after_seconds


class SerialConnection(Protocol):
    def write(self, data: bytes) -> int: ...

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


PathResolver = Callable[[str], str]
SerialFactory = Callable[[str], SerialConnection]
Clock = Callable[[], float]
ConnectHook = Callable[[SerialConnection], None]
Result = TypeVar("Result")


def encode_grayscale_frame(frame: Sequence[Sequence[int]]) -> tuple[bytes, ...]:
    """Encode a row-major 9x34 frame as the Framework stage-column and commit commands."""
    if len(frame) != FRAME_HEIGHT:
        raise ValueError(f"grayscale frame must have {FRAME_HEIGHT} rows")

    for row in frame:
        if len(row) != FRAME_WIDTH:
            raise ValueError(f"each grayscale frame row must have {FRAME_WIDTH} columns")
        for pixel in row:
            if isinstance(pixel, bool) or not isinstance(pixel, int) or not 0 <= pixel <= 255:
                raise ValueError("grayscale pixels must be integers between 0 and 255")

    stage_commands = tuple(
        _FRAMEWORK_MAGIC
        + bytes([_STAGE_GREYSCALE_COLUMN, x])
        + bytes(frame[y][x] for y in range(FRAME_HEIGHT))
        for x in range(FRAME_WIDTH)
    )
    commit_command = _FRAMEWORK_MAGIC + bytes([_DRAW_GREYSCALE_BUFFER, 0x00])
    return (*stage_commands, commit_command)


def brightness_command(brightness_percent: int) -> bytes:
    if (
        isinstance(brightness_percent, bool)
        or not isinstance(brightness_percent, int)
        or not 0 <= brightness_percent <= 100
    ):
        raise ValueError("brightness_percent must be an integer between 0 and 100")
    level = max(MIN_BRIGHTNESS_LEVEL, round(255 * brightness_percent / 100))
    return _FRAMEWORK_MAGIC + bytes([_BRIGHTNESS, level])


def sleep_command(sleeping: bool) -> bytes:
    """Sleep fades the panel out and powers its LED controller down; any later command wakes it."""
    return _FRAMEWORK_MAGIC + bytes([_SLEEP, 1 if sleeping else 0])


def prepare_panel(brightness_percent: Callable[[], int]) -> ConnectHook:
    """Build the setup run on every new connection: fixed brightness, no firmware animation.

    Any command wakes a sleeping panel, which then ignores USB during its fade-in. The setup
    therefore wakes it with a GetBrightness query and waits for the answer by reading: a read
    just waits, while a second write would block in the serial layer and hit its timeout.
    """
    query = _FRAMEWORK_MAGIC + bytes([_BRIGHTNESS])
    animation_off = _FRAMEWORK_MAGIC + bytes([_ANIMATE, 0x00])

    def prepare(connection: SerialConnection) -> None:
        _write_exact(connection, query)
        response = connection.read(_RESPONSE_SIZE)
        if len(response) != _RESPONSE_SIZE:
            raise OSError(
                f"panel did not answer the brightness query: got {len(response)} of "
                f"{_RESPONSE_SIZE} bytes"
            )
        _write_exact(connection, brightness_command(brightness_percent()))
        _write_exact(connection, animation_off)

    return prepare


class MatrixTransport:
    """Persistent serial connection that reconnects by stable panel identity."""

    def __init__(
        self,
        *,
        identity: str,
        path_resolver: PathResolver,
        serial_factory: SerialFactory | None = None,
        clock: Clock = time.monotonic,
        reconnect_initial_seconds: float = 0.25,
        reconnect_max_seconds: float = 5.0,
        on_connect: ConnectHook | None = None,
    ) -> None:
        if reconnect_initial_seconds <= 0:
            raise ValueError("reconnect_initial_seconds must be greater than zero")
        if reconnect_max_seconds < reconnect_initial_seconds:
            raise ValueError("reconnect_max_seconds must not be below the initial delay")

        self._identity = identity
        self._path_resolver = path_resolver
        self._serial_factory = serial_factory or _open_serial
        self._clock = clock
        self._reconnect_initial_seconds = reconnect_initial_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._on_connect = on_connect
        self._connection: SerialConnection | None = None
        self._failure_count = 0
        self._next_attempt_at = 0.0

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    def send(self, command: bytes) -> None:
        self._execute(lambda connection: _write_exact(connection, command))

    def write_frame(self, frame: Sequence[Sequence[int]]) -> None:
        """Send each command as its own write so every command lands in its own USB packet.

        Joined commands share a packet and the firmware silently drops all but the first.
        The firmware paces the host through USB flow control, so no sleep is needed.
        """
        commands = encode_grayscale_frame(frame)

        def write_commands(connection: SerialConnection) -> None:
            for command in commands:
                _write_exact(connection, command)

        self._execute(write_commands)

    def exchange(self, command: bytes, *, response_size: int) -> bytes:
        if response_size <= 0:
            raise ValueError("response_size must be greater than zero")

        def write_then_read(connection: SerialConnection) -> bytes:
            _write_exact(connection, command)
            response = connection.read(response_size)
            if len(response) != response_size:
                raise OSError(f"short serial read: expected {response_size}, got {len(response)}")
            return response

        return self._execute(write_then_read)

    def close(self) -> None:
        self._disconnect()

    def _execute(self, operation: Callable[[SerialConnection], Result]) -> Result:
        self._ensure_reconnect_ready()
        try:
            connection = self._connection or self._connect()
            result = operation(connection)
        except (DeviceDiscoveryError, OSError) as error:
            self._disconnect()
            self._schedule_reconnect()
            raise TransportError(f"matrix {self._identity!r} transport failed: {error}") from error

        self._failure_count = 0
        self._next_attempt_at = 0.0
        return result

    def _connect(self) -> SerialConnection:
        path = self._path_resolver(self._identity)
        self._connection = self._serial_factory(path)
        if self._on_connect is not None:
            self._on_connect(self._connection)
        return self._connection

    def _ensure_reconnect_ready(self) -> None:
        retry_after = self._next_attempt_at - self._clock()
        if retry_after > 0:
            raise ReconnectPending(retry_after)

    def _schedule_reconnect(self) -> None:
        delay = min(
            self._reconnect_initial_seconds * (2**self._failure_count),
            self._reconnect_max_seconds,
        )
        self._failure_count += 1
        self._next_attempt_at = self._clock() + delay

    def _disconnect(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except OSError:
            pass


def _write_exact(connection: SerialConnection, command: bytes) -> None:
    written = connection.write(command)
    if written != len(command):
        raise OSError(f"short serial write: expected {len(command)}, wrote {written}")


def _open_serial(path: str) -> SerialConnection:
    return SerialPort(
        path,
        read_timeout_seconds=_SERIAL_READ_TIMEOUT_SECONDS,
        write_timeout_seconds=_SERIAL_WRITE_TIMEOUT_SECONDS,
    )
