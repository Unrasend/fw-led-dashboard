from __future__ import annotations

import pytest

from framework_led_dashboard import matrix_transport
from framework_led_dashboard.matrix_transport import (
    MatrixTransport,
    ReconnectPending,
    TransportError,
    brightness_command,
    encode_grayscale_frame,
    prepare_panel,
    sleep_command,
)

# ledmatrix firmware parses one command per USB packet and drops the rest of the packet.
FIRMWARE_USB_PACKET_SIZE = 64


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
    assert serial_connection.writes == [*encode_grayscale_frame(frame)] * 2
    assert transport.is_connected is True

    transport.close()
    assert serial_connection.closed is True


def test_transport_writes_each_command_separately_so_firmware_sees_every_command() -> None:
    serial_connection = FakeSerial()
    transport = MatrixTransport(
        identity="stable-left",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
        serial_factory=lambda path: serial_connection,
    )
    frame = [[(x + y) % 256 for x in range(9)] for y in range(34)]

    transport.write_frame(frame)

    assert serial_connection.writes == list(encode_grayscale_frame(frame))
    assert len(serial_connection.writes) == 10
    assert all(len(write) <= FIRMWARE_USB_PACKET_SIZE for write in serial_connection.writes)


def test_default_serial_timeout_tolerates_observed_framework_usb_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeSerial()
    opened_with: dict[str, object] = {}

    def open_serial(path: str, **options: object) -> FakeSerial:
        opened_with.update(path=path, **options)
        return connection

    monkeypatch.setattr(matrix_transport, "SerialPort", open_serial)
    transport = MatrixTransport(
        identity="stable-left",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
    )

    transport.write_frame([[0] * 9 for _ in range(34)])

    # A waking panel ignores USB during its fade-in: 255 / 5 steps x 100 ms = 5.1 s.
    assert opened_with["path"] == "/dev/by-id/stable-left"
    assert opened_with["write_timeout_seconds"] >= 5.2
    assert opened_with["read_timeout_seconds"] >= 5.2


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
    assert second_connection.writes == list(encode_grayscale_frame(frame))
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


class AnswerTracingSerial(FakeSerial):
    """Record writes and reads in one sequence to check what is written before the answer."""

    def __init__(self) -> None:
        super().__init__(read_result=bytes(32))
        self.events: list[str] = []

    def write(self, data: bytes) -> int:
        self.events.append(f"write {data.hex()}")
        return super().write(data)

    def read(self, size: int) -> bytes:
        self.events.append(f"read {size}")
        return super().read(size)


def test_prepare_panel_wakes_with_a_query_and_waits_for_its_answer_before_setup() -> None:
    connection = AnswerTracingSerial()

    prepare_panel(lambda: 40)(connection)

    # The query is the only write before the read: any later write would block during the
    # firmware's wake-up fade, while the read simply waits for the answer that follows it.
    assert connection.events == [
        "write 32ac00",
        "read 32",
        "write 32ac0066",
        "write 32ac0400",
    ]


@pytest.mark.parametrize(("percent", "level"), [(0, 5), (1, 5), (2, 5), (3, 8), (40, 102), (100, 255)])
def test_prepare_panel_scales_brightness_percent_to_firmware_level(
    percent: int,
    level: int,
) -> None:
    connection = FakeSerial(read_result=bytes(32))

    prepare_panel(lambda: percent)(connection)

    assert connection.writes[1] == bytes([0x32, 0xAC, 0x00, level])


def test_prepare_panel_rejects_a_panel_that_does_not_answer() -> None:
    with pytest.raises(OSError, match="did not answer"):
        prepare_panel(lambda: 40)(FakeSerial(read_result=b"short"))


def test_brightness_never_goes_below_the_firmware_fade_step() -> None:
    # ledmatrix fades a waking panel in with `old_brightness - 5` on a u8: a stored level below 5
    # wraps to ~250, so the panel flares to full brightness before dropping back.
    assert all(brightness_command(percent)[3] >= 5 for percent in range(101))


@pytest.mark.parametrize("percent", [-1, 101, True, 40.0])
def test_brightness_command_rejects_invalid_percent(percent: object) -> None:
    with pytest.raises(ValueError, match="brightness"):
        brightness_command(percent)


def test_prepare_panel_uses_the_brightness_current_at_each_connection() -> None:
    brightness = [40]
    prepare = prepare_panel(lambda: brightness[0])
    first = FakeSerial(read_result=bytes(32))
    second = FakeSerial(read_result=bytes(32))

    prepare(first)
    brightness[0] = 100
    prepare(second)

    assert first.writes[1] == brightness_command(40)
    assert second.writes[1] == brightness_command(100)


def test_sleep_command_matches_firmware_protocol() -> None:
    assert sleep_command(True) == bytes([0x32, 0xAC, 0x03, 0x01])
    assert sleep_command(False) == bytes([0x32, 0xAC, 0x03, 0x00])


def test_transport_sends_a_single_command_as_one_write() -> None:
    connection = FakeSerial()
    transport = MatrixTransport(
        identity="stable-left",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
        serial_factory=lambda path: connection,
    )

    transport.send(brightness_command(60))

    assert connection.writes == [bytes([0x32, 0xAC, 0x00, 153])]


def test_transport_prepares_every_new_connection_before_its_first_frame() -> None:
    clock = FakeClock()
    first_connection = FakeSerial(fail_write_at=0)
    second_connection = FakeSerial()
    connections = iter([first_connection, second_connection])
    prepared: list[FakeSerial] = []

    def on_connect(connection: FakeSerial) -> None:
        assert connection.writes == []
        prepared.append(connection)

    transport = MatrixTransport(
        identity="stable-left",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
        serial_factory=lambda path: next(connections),
        clock=clock,
        reconnect_initial_seconds=1,
        on_connect=on_connect,
    )
    frame = [[0] * 9 for _ in range(34)]

    with pytest.raises(TransportError):
        transport.write_frame(frame)
    clock.now = 1
    transport.write_frame(frame)
    transport.write_frame(frame)

    assert prepared == [first_connection, second_connection]


def test_failed_panel_preparation_closes_connection_and_backs_off() -> None:
    clock = FakeClock()
    connection = FakeSerial()

    def on_connect(_connection: FakeSerial) -> None:
        raise OSError("panel did not answer")

    transport = MatrixTransport(
        identity="stable-right",
        path_resolver=lambda identity: f"/dev/by-id/{identity}",
        serial_factory=lambda path: connection,
        clock=clock,
        reconnect_initial_seconds=1,
        on_connect=on_connect,
    )

    with pytest.raises(TransportError, match="did not answer"):
        transport.write_frame([[0] * 9 for _ in range(34)])
    assert connection.closed is True
    assert connection.writes == []
    with pytest.raises(ReconnectPending):
        transport.write_frame([[0] * 9 for _ in range(34)])
