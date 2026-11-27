from __future__ import annotations

import json
import logging
import socket
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from framework_led_dashboard.control import (
    MAX_MESSAGE_BYTES,
    ControlClient,
    ControlError,
    ControlServer,
    DisplaySettings,
    SettingsStore,
    decode_frame,
    default_socket_path,
    encode_frame,
    handle_request,
)
from framework_led_dashboard.frame import FRAME_HEIGHT, FRAME_WIDTH
from framework_led_dashboard.layout import (
    DEFAULT_LAYOUT,
    Layout,
    bars,
    classic_left,
    classic_right,
    default_layout,
    equalizer,
    space,
    temperature,
)
from framework_led_dashboard.scheduler import run_loop

SMALL_LAYOUT = Layout(left=(temperature("gpu"), space(2), bars(("disk", "gpu", "battery"), 9)), right=(equalizer(5),))
FRAMES = {
    "left": tuple(tuple((x + y) % 256 for x in range(FRAME_WIDTH)) for y in range(FRAME_HEIGHT)),
    "right": tuple(tuple(255 if x == y % FRAME_WIDTH else 0 for x in range(FRAME_WIDTH)) for y in range(FRAME_HEIGHT)),
}


@pytest.fixture
def short_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Unix socket paths are limited to ~107 bytes; keep them short.
    return tmp_path_factory.mktemp("c")


@pytest.fixture
def running_server(short_dir: Path) -> Iterator[tuple[ControlServer, SettingsStore, Path]]:
    store = SettingsStore(DisplaySettings(40))
    path = short_dir / "d.sock"
    server = ControlServer(store, path=path, frames=lambda: FRAMES, accept_timeout_seconds=0.05)
    server.open()
    stop = threading.Event()
    thread = threading.Thread(target=run_loop, args=(server.step, stop), kwargs={"name": "control"})
    thread.start()
    try:
        yield server, store, path
    finally:
        stop.set()
        thread.join(2.0)
        server.close()


@pytest.mark.parametrize(
    ("brightness", "enabled"),
    [(-1, True), (101, True), (True, True), (40.0, True), (40, "yes"), (40, 1)],
)
def test_display_settings_reject_invalid_values(brightness: object, enabled: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        DisplaySettings(brightness, enabled)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"layout": {"left": [], "right": []}}, "layout"),
        ({"audio_source": "line-in"}, "audio_source"),
        ({"audio_source": None}, "audio_source"),
    ],
)
def test_display_settings_reject_a_bad_layout_or_sound_source(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        DisplaySettings(40, **changes)


def test_swapped_panels_show_each_others_widgets() -> None:
    settings = DisplaySettings(40, layout=SMALL_LAYOUT)

    assert settings.widgets_on("left") == SMALL_LAYOUT.left
    assert DisplaySettings(40, layout=SMALL_LAYOUT, swap_panels=True).widgets_on("left") == (
        SMALL_LAYOUT.right
    )


@pytest.mark.parametrize(
    ("alert", "error"),
    [(True, TypeError), ("90", TypeError), (39, ValueError), (151, ValueError),
     (float("nan"), ValueError)],
)
def test_display_settings_reject_a_cpu_alert_outside_40_to_150(
    alert: object, error: type[Exception]
) -> None:
    with pytest.raises(error, match="cpu_alert_celsius"):
        DisplaySettings(40, cpu_alert_celsius=alert)


@pytest.mark.parametrize("swap_panels", ["yes", 1, None])
def test_display_settings_reject_a_swap_that_is_not_a_boolean(swap_panels: object) -> None:
    with pytest.raises(TypeError, match="swap_panels"):
        DisplaySettings(40, swap_panels=swap_panels)


def test_settings_store_loads_defaults_when_no_file_exists(tmp_path: Path) -> None:
    store = SettingsStore.load(tmp_path / "display.json", default_brightness_percent=40)

    assert store.current() == DisplaySettings(40, enabled=True)


def test_settings_store_persists_updates_and_reloads_them(tmp_path: Path) -> None:
    path = tmp_path / "state" / "display.json"
    store = SettingsStore.load(path, default_brightness_percent=40)

    assert store.update(brightness_percent=70) == DisplaySettings(70, enabled=True)
    assert store.update(enabled=False) == DisplaySettings(70, enabled=False)

    reloaded = SettingsStore.load(path, default_brightness_percent=40)
    assert reloaded.current() == DisplaySettings(70, enabled=False)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {
        "brightness_percent": 70,
        "enabled": False,
        "swap_panels": False,
        "cpu_alert_celsius": 120,
        "layout": DEFAULT_LAYOUT.to_json(),
        "audio_source": "microphone",
    }


def test_settings_store_persists_the_layout_and_the_sound_source(tmp_path: Path) -> None:
    path = tmp_path / "display.json"
    store = SettingsStore.load(path, default_brightness_percent=40)

    store.update(layout=SMALL_LAYOUT, audio_source="system")

    reloaded = SettingsStore.load(path, default_brightness_percent=40).current()
    assert (reloaded.layout, reloaded.audio_source) == (SMALL_LAYOUT, "system")


def test_settings_store_reads_a_file_saved_before_bars_existed(tmp_path: Path) -> None:
    path = tmp_path / "display.json"
    path.write_text('{"brightness_percent": 55, "enabled": false}', encoding="utf-8")

    store = SettingsStore.load(path, default_brightness_percent=40)

    assert store.current() == DisplaySettings(55, enabled=False, layout=DEFAULT_LAYOUT)


def test_settings_store_moves_the_bars_of_a_file_from_before_layouts_into_the_default_layout(
    tmp_path: Path,
) -> None:
    path = tmp_path / "display.json"
    path.write_text(
        json.dumps(
            {"brightness_percent": 55, "enabled": True, "bars": ["gpu", "cpu", "disk"],
             "swap_panels": True, "cpu_alert_celsius": 95}
        ),
        encoding="utf-8",
    )

    loaded = SettingsStore.load(path, default_brightness_percent=40).current()

    assert loaded == DisplaySettings(
        55, swap_panels=True, cpu_alert_celsius=95, layout=default_layout(("gpu", "cpu", "disk"))
    )


def test_settings_store_starts_with_the_configured_swap_and_then_keeps_the_trays(
    tmp_path: Path,
) -> None:
    path = tmp_path / "display.json"
    store = SettingsStore.load(path, default_brightness_percent=40, default_swap_panels=True)
    assert store.current().swap_panels is True

    store.update(swap_panels=False)

    # The saved choice wins over the config from now on, like the brightness does.
    reloaded = SettingsStore.load(path, default_brightness_percent=40, default_swap_panels=True)
    assert reloaded.current().swap_panels is False


def test_settings_store_reads_a_file_saved_before_swapping_existed(tmp_path: Path) -> None:
    path = tmp_path / "display.json"
    path.write_text(
        json.dumps({"brightness_percent": 55, "enabled": True, "bars": ["cpu", "ram", "gpu"]}),
        encoding="utf-8",
    )

    for configured in (False, True):
        loaded = SettingsStore.load(
            path, default_brightness_percent=40, default_swap_panels=configured
        ).current()
        assert loaded == DisplaySettings(
            55, swap_panels=configured, layout=default_layout(("cpu", "ram", "gpu"))
        )


def test_settings_store_starts_with_the_configured_cpu_alert_and_keeps_the_trays(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "display.json"
    path.write_text(json.dumps({"brightness_percent": 40, "enabled": True}), encoding="utf-8")
    store = SettingsStore.load(path, default_brightness_percent=40, default_cpu_alert_celsius=110)
    assert store.current().cpu_alert_celsius == 110  # saved by an older version: config decides

    with caplog.at_level(logging.INFO):
        store.update(cpu_alert_celsius=92)

    reloaded = SettingsStore.load(
        path, default_brightness_percent=40, default_cpu_alert_celsius=110
    )
    assert reloaded.current().cpu_alert_celsius == 92
    assert [record.getMessage() for record in caplog.records] == ["CPU temperature alert at 92 °C"]


def test_settings_store_logs_swaps_layouts_and_sound_sources(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SettingsStore.load(tmp_path / "display.json", default_brightness_percent=40)
    classic = Layout(left=classic_left(), right=classic_right())

    with caplog.at_level(logging.INFO):
        store.update(swap_panels=True)
        store.update(swap_panels=True)  # no change, no message
        store.update(swap_panels=False)
        store.update(layout=classic)
        store.update(audio_source="system")

    assert [record.getMessage() for record in caplog.records] == [
        "panels swapped",
        "panels not swapped",
        (
            "layout: left bars, line, space, equalizer; right weather icon, space, outside "
            "temperature, space, cpu temperature, space, equalizer"
        ),
        "equalizer source: system",
    ]


def test_settings_store_keeps_old_value_after_invalid_update(tmp_path: Path) -> None:
    store = SettingsStore.load(tmp_path / "display.json", default_brightness_percent=40)

    with pytest.raises(ValueError):
        store.update(brightness_percent=150)

    assert store.current() == DisplaySettings(40)
    assert not (tmp_path / "display.json").exists()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"brightness_percent": 300, "enabled": true}',
        '{"enabled": true}',
        '{"brightness_percent": 40, "enabled": true, "layout": {"left": [{"kind": "clock"}], "right": []}}',
    ],
)
def test_settings_store_ignores_a_corrupt_file(
    tmp_path: Path,
    content: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "display.json"
    path.write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        store = SettingsStore.load(path, default_brightness_percent=40)

    assert store.current() == DisplaySettings(40)
    assert "ignoring display settings" in caplog.text


def test_handle_request_reports_and_changes_settings() -> None:
    store = SettingsStore(DisplaySettings(40))

    assert handle_request(store, {"command": "status"}) == {
        "ok": True,
        "brightness_percent": 40,
        "enabled": True,
        "swap_panels": False,
        "cpu_alert_celsius": 120,
        "layout": DEFAULT_LAYOUT.to_json(),
        "audio_source": "microphone",
        "panels": ["left", "right"],
    }
    assert (
        handle_request(store, {"command": "set", "brightness_percent": 65})["brightness_percent"]
        == 65
    )
    assert handle_request(store, {"command": "set", "enabled": False})["enabled"] is False
    assert handle_request(store, {"command": "set", "swap_panels": True})["swap_panels"] is True
    changed = handle_request(store, {"command": "set", "cpu_alert_celsius": 95})
    assert changed["cpu_alert_celsius"] == 95
    changed = handle_request(store, {"command": "set", "layout": SMALL_LAYOUT.to_json()})
    assert changed["layout"] == SMALL_LAYOUT.to_json()
    assert store.current().layout == SMALL_LAYOUT
    changed = handle_request(store, {"command": "set", "audio_source": "system"})
    assert changed["audio_source"] == "system"


def test_frames_are_sent_as_hex_and_come_back_unchanged() -> None:
    store = SettingsStore(DisplaySettings(40))

    response = handle_request(store, {"command": "frames"}, frames=lambda: FRAMES)

    assert response["ok"] is True
    assert {side: decode_frame(response[side]) for side in ("left", "right")} == FRAMES
    assert len(encode_frame(FRAMES["left"])) == 2 * FRAME_WIDTH * FRAME_HEIGHT


def test_a_dashboard_with_one_module_names_it_and_sends_only_its_frame() -> None:
    store = SettingsStore(DisplaySettings(40))
    only_right = {"right": FRAMES["right"]}

    status = handle_request(store, {"command": "status"}, panels=("right",))
    changed = handle_request(store, {"command": "set", "enabled": False}, panels=("right",))
    frames = handle_request(
        store, {"command": "frames"}, frames=lambda: only_right, panels=("right",)
    )

    assert status["panels"] == changed["panels"] == ["right"]
    assert frames == {"ok": True, "right": encode_frame(FRAMES["right"])}


def test_client_learns_which_modules_the_dashboard_drives(short_dir: Path) -> None:
    store = SettingsStore(DisplaySettings(40))
    path = short_dir / "one.sock"
    server = ControlServer(
        store,
        path=path,
        frames=lambda: {"left": FRAMES["left"]},
        panels=("left",),
        accept_timeout_seconds=0.05,
    )
    server.open()
    stop = threading.Event()
    thread = threading.Thread(target=run_loop, args=(server.step, stop), kwargs={"name": "control"})
    thread.start()
    try:
        client = ControlClient(path)
        assert client.panels == ("left", "right")  # until the dashboard says otherwise
        client.status()
        assert client.panels == ("left",)
        assert client.set(brightness_percent=70) == DisplaySettings(70)
        assert client.panels == ("left",)
        assert client.frames() == {"left": FRAMES["left"]}
    finally:
        stop.set()
        thread.join(2.0)
        server.close()


def test_frames_need_a_dashboard_that_renders_them() -> None:
    response = handle_request(SettingsStore(DisplaySettings(40)), {"command": "frames"})

    assert response["ok"] is False


@pytest.mark.parametrize("text", ["", "zz", "00" * (FRAME_WIDTH * FRAME_HEIGHT - 1)])
def test_decode_frame_rejects_what_is_not_a_frame(text: str) -> None:
    with pytest.raises(ValueError):
        decode_frame(text)


@pytest.mark.parametrize(
    ("request_body", "message"),
    [
        ([], "JSON object"),
        ({"command": "reboot"}, "command"),
        ({"command": "set", "color": "red"}, "unknown fields: color"),
        ({"command": "set", "brightness_percent": "max"}, "brightness_percent"),
        ({"command": "set", "enabled": "on"}, "enabled"),
        ({"command": "set", "bars": ["cpu", "ram", "gpu"]}, "unknown fields: bars"),
        ({"command": "set", "layout": "default"}, "layout"),
        ({"command": "set", "layout": {"left": [], "right": [], "top": []}}, "unknown fields: top"),
        ({"command": "set", "layout": {"left": [{"kind": "clock"}], "right": []}}, "clock"),
        (
            {"command": "set", "layout": {"left": [{"kind": "equalizer", "height": 30}] * 2, "right": []}},
            "rows",
        ),
        ({"command": "set", "audio_source": "line-in"}, "audio_source"),
        ({"command": "set", "swap_panels": "yes"}, "swap_panels"),
        ({"command": "set", "swap_panels": 1}, "swap_panels"),
        ({"command": "set", "cpu_alert_celsius": 20}, "cpu_alert_celsius"),
        ({"command": "set", "cpu_alert_celsius": "hot"}, "cpu_alert_celsius"),
    ],
)
def test_handle_request_rejects_bad_requests(request_body: object, message: str) -> None:
    store = SettingsStore(DisplaySettings(40))

    response = handle_request(store, request_body)

    assert response["ok"] is False
    assert message in str(response["error"])
    assert store.current() == DisplaySettings(40)


def test_client_and_server_round_trip(
    running_server: tuple[ControlServer, SettingsStore, Path],
) -> None:
    _server, store, path = running_server
    client = ControlClient(path)

    assert client.status() == DisplaySettings(40)
    assert client.set(brightness_percent=80) == DisplaySettings(80)
    assert client.set(enabled=False) == DisplaySettings(80, enabled=False)
    assert client.set(layout=SMALL_LAYOUT).layout == SMALL_LAYOUT
    assert client.set(swap_panels=True).swap_panels is True
    assert client.set(cpu_alert_celsius=88).cpu_alert_celsius == 88
    assert client.set(audio_source="system").audio_source == "system"
    assert store.current() == DisplaySettings(
        80,
        enabled=False,
        swap_panels=True,
        cpu_alert_celsius=88,
        layout=SMALL_LAYOUT,
        audio_source="system",
    )
    assert client.frames() == FRAMES
    assert client.panels == ("left", "right")


def test_client_surfaces_rejected_request(
    running_server: tuple[ControlServer, SettingsStore, Path],
) -> None:
    _server, _store, path = running_server

    with pytest.raises(ControlError, match="brightness_percent"):
        ControlClient(path).set(brightness_percent=500)


def test_server_rejects_oversized_message(
    running_server: tuple[ControlServer, SettingsStore, Path],
) -> None:
    _server, _store, path = running_server

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2.0)
        connection.connect(str(path))
        connection.sendall(b"x" * (MAX_MESSAGE_BYTES + 10))
        answer = json.loads(connection.makefile("rb").readline())

    assert answer["ok"] is False
    assert "too large" in answer["error"]


def test_socket_is_private_to_the_user(
    running_server: tuple[ControlServer, SettingsStore, Path],
) -> None:
    _server, _store, path = running_server

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_client_reports_unreachable_dashboard(short_dir: Path) -> None:
    with pytest.raises(ControlError, match="not reachable"):
        ControlClient(short_dir / "missing.sock").status()


def test_second_server_refuses_a_socket_that_is_still_answering(
    running_server: tuple[ControlServer, SettingsStore, Path],
) -> None:
    _server, _store, path = running_server
    second = ControlServer(SettingsStore(DisplaySettings(10)), path=path)

    with pytest.raises(ControlError, match="already"):
        second.open()


def test_server_makes_a_private_folder_for_its_socket(short_dir: Path) -> None:
    path = short_dir / "folder" / "c.sock"
    server = ControlServer(SettingsStore(DisplaySettings(40)), path=path)

    server.open()
    try:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_ISSOCK(path.stat().st_mode)
    finally:
        server.close()


def test_default_socket_sits_in_a_folder_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")

    assert default_socket_path() == Path("/run/user/4242/framework-led-dashboard/control.sock")


def test_server_replaces_a_stale_socket_and_removes_it_on_close(short_dir: Path) -> None:
    path = short_dir / "d.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    server = ControlServer(SettingsStore(DisplaySettings(40)), path=path)

    server.open()
    assert path.exists()
    server.close()

    assert not path.exists()
