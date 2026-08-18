from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from framework_led_dashboard.matrix_transport import (
    MatrixTransport,
    ReconnectPending,
    TransportError,
    encode_grayscale_frame,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeSerial:
    def __init__(
        self,
        *,
        fail_write_at: int | None = None,
        read_result: bytes = b"",
        read_error: OSError | None = None,
    ) -> None:
        self.fail_write_at = fail_write_at
        self.read_result = read_result
        self.read_error = read_error
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.fail_write_at == len(self.writes):
            raise OSError("device disappeared")
        self.writes.append(data)
        return len(data)

    def read(self, size: int) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.read_result[:size]

    def close(self) -> None:
        self.closed = True


def test_grayscale_frame_matches_official_stage_and_commit_protocol() -> None:
    frame = [[(x * 34 + y) % 256 for x in range(9)] for y in range(34)]

    commands = encode_grayscale_frame(frame)

    expected_stage_commands = tuple(
        bytes([0x32, 0xAC, 0x07, x, *[(x * 34 + y) % 256 for y in range(34)]])
        for x in range(9)
    )
    assert commands == (*expected_stage_commands, bytes([0x32, 0xAC, 0x08, 0x00]))


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        ([[0] * 9 for _ in range(33)], "34 rows"),
        ([[0] * 8 for _ in range(34)], "9 columns"),
        ([[256] * 9 for _ in range(34)], "between 0 and 255"),
        ([[True] * 9 for _ in range(34)], "between 0 and 255"),
    ],
)
def test_grayscale_frame_rejects_invalid_dimensions_and_pixels(
    frame: list[list[int]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        encode_grayscale_frame(frame)


def test_transport_keeps_one_serial_connection_for_multiple_frames() -> None:
    serial_connection = FakeSerial()
    opened_paths: list[str] = []

    def serial_factory(path: str) -> FakeSerial:
        opened_paths.append(path)
        return serial_connection

    transport = MatrixTransport(
        identity="stable-left",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
        serial_factory=serial_factory,
    )
    frame = [[0] * 9 for _ in range(34)]

    transport.write_frame(frame)
    transport.write_frame(frame)

    assert opened_paths == ["/dev/by-id/stable-left"]
    assert len(serial_connection.writes) == 20
    assert transport.is_connected is True

    transport.close()
    assert serial_connection.closed is True


def test_default_serial_timeout_tolerates_observed_framework_usb_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeSerial()
    opened_with: dict[str, object] = {}

    def open_serial(path: str, baud_rate: int, **options: object) -> FakeSerial:
        opened_with.update(path=path, baud_rate=baud_rate, **options)
        return connection

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=open_serial))
    transport = MatrixTransport(
        identity="stable-left",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
    )

    transport.write_frame([[0] * 9 for _ in range(34)])

    assert opened_with["write_timeout"] >= 2.0


def test_write_error_closes_connection_then_resolves_same_identity_after_backoff() -> None:
    clock = FakeClock()
    current_path = ["/dev/ttyACM0"]
    resolved_identities: list[str] = []
    first_connection = FakeSerial(fail_write_at=2)
    second_connection = FakeSerial()
    connections = iter([first_connection, second_connection])

    def resolve_path(identity: str) -> str:
        resolved_identities.append(identity)
        return current_path[0]

    transport = MatrixTransport(
        identity="stable-left",
        path_resolver=resolve_path,
        serial_factory=lambda path: next(connections),
        clock=clock,
        reconnect_initial_seconds=1,
        reconnect_max_seconds=4,
    )
    frame = [[0] * 9 for _ in range(34)]

    with pytest.raises(TransportError, match="stable-left"):
        transport.write_frame(frame)
    assert first_connection.closed is True

    with pytest.raises(ReconnectPending) as pending:
        transport.write_frame(frame)
    assert pending.value.retry_after_seconds == pytest.approx(1)

    current_path[0] = "/dev/ttyACM1"
    clock.now = 1
    transport.write_frame(frame)

    assert resolved_identities == ["stable-left", "stable-left"]
    assert len(second_connection.writes) == 10
    assert transport.is_connected is True


def test_open_failures_use_exponential_backoff_capped_at_configured_maximum() -> None:
    clock = FakeClock()
    open_attempts = 0

    def failing_factory(path: str) -> FakeSerial:
        nonlocal open_attempts
        open_attempts += 1
        raise OSError(f"cannot open {path}")

    transport = MatrixTransport(
        identity="stable-left",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
        serial_factory=failing_factory,
        clock=clock,
        reconnect_initial_seconds=1,
        reconnect_max_seconds=4,
    )
    frame = [[0] * 9 for _ in range(34)]

    for attempt_time in (0, 1, 3, 7):
        clock.now = attempt_time
        with pytest.raises(TransportError):
            transport.write_frame(frame)

    clock.now = 10.9
    with pytest.raises(ReconnectPending) as pending:
        transport.write_frame(frame)
    assert pending.value.retry_after_seconds == pytest.approx(0.1)

    clock.now = 11
    with pytest.raises(TransportError):
        transport.write_frame(frame)
    assert open_attempts == 5


@pytest.mark.parametrize(
    "connection",
    [
        FakeSerial(read_error=OSError("read failed")),
        FakeSerial(read_result=b"short"),
    ],
)
def test_read_errors_close_connection_and_enter_reconnect_state(connection: FakeSerial) -> None:
    transport = MatrixTransport(
        identity="stable-right",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
        serial_factory=lambda path: connection,
    )

    with pytest.raises(TransportError):
        transport.exchange(bytes([0x32, 0xAC, 0x20]), response_size=32)

    assert connection.closed is True
    assert transport.is_connected is False
