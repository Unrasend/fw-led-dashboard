from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar

from .device_discovery import DeviceDiscoveryError
from .frame import FRAME_HEIGHT, FRAME_WIDTH

_FRAMEWORK_MAGIC = bytes([0x32, 0xAC])
_STAGE_GREYSCALE_COLUMN = 0x07
_DRAW_GREYSCALE_BUFFER = 0x08
_BAUD_RATE = 115_200
_SERIAL_READ_TIMEOUT_SECONDS = 0.1
_SERIAL_WRITE_TIMEOUT_SECONDS = 2.0
_INTER_COMMAND_DELAY_SECONDS = 0.02


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
Sleeper = Callable[[float], None]
Result = TypeVar("Result")


def encode_grayscale_frame(frame: Sequence[Sequence[int]]) -> tuple[bytes, ...]:
    """Encode a row-major 9x34 frame as one atomic Framework matrix update."""
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


class MatrixTransport:
    """Persistent serial connection that reconnects by stable panel identity."""

    def __init__(
        self,
        *,
        identity: str,
        path_resolver: PathResolver,
        serial_factory: SerialFactory | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
        reconnect_initial_seconds: float = 0.25,
        reconnect_max_seconds: float = 5.0,
    ) -> None:
        if reconnect_initial_seconds <= 0:
            raise ValueError("reconnect_initial_seconds must be greater than zero")
        if reconnect_max_seconds < reconnect_initial_seconds:
            raise ValueError("reconnect_max_seconds must not be below the initial delay")

        self._identity = identity
        self._path_resolver = path_resolver
        self._serial_factory = serial_factory or _open_serial
        self._clock = clock
        self._sleeper = sleeper
        self._reconnect_initial_seconds = reconnect_initial_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._connection: SerialConnection | None = None
        self._failure_count = 0
        self._next_attempt_at = 0.0

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    def write_frame(self, frame: Sequence[Sequence[int]]) -> None:
        commands = encode_grayscale_frame(frame)

        def write_commands(connection: SerialConnection) -> None:
            for index, command in enumerate(commands):
                _write_exact(connection, command)
                if index < len(commands) - 1:
                    self._sleeper(_INTER_COMMAND_DELAY_SECONDS)

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
    import serial

    return serial.Serial(
        path,
        _BAUD_RATE,
        timeout=_SERIAL_READ_TIMEOUT_SECONDS,
        write_timeout=_SERIAL_WRITE_TIMEOUT_SECONDS,
    )
