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
    handle_request,
)
from framework_led_dashboard.scheduler import run_loop


@pytest.fixture
def short_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Unix socket paths are limited to ~107 bytes; keep them short.
    return tmp_path_factory.mktemp("c")


@pytest.fixture
def running_server(short_dir: Path) -> Iterator[tuple[ControlServer, SettingsStore, Path]]:
    store = SettingsStore(DisplaySettings(40))
    path = short_dir / "d.sock"
    server = ControlServer(store, path=path, accept_timeout_seconds=0.05)
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
    "bars",
    [("cpu", "ram"), ("cpu", "ram", "fan"), ["cpu", "ram", "gpu"], ("cpu", "ram", 3)],
)
def test_display_settings_reject_invalid_bars(bars: object) -> None:
    with pytest.raises((TypeError, ValueError), match="bars"):
        DisplaySettings(40, bars=bars)


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
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "brightness_percent": 70,
        "enabled": False,
        "bars": ["battery", "cpu", "ram"],
    }


def test_settings_store_persists_bar_choice(tmp_path: Path) -> None:
    path = tmp_path / "display.json"
    store = SettingsStore.load(path, default_brightness_percent=40)

    assert store.update(bars=("gpu", "cpu", "disk")).bars == ("gpu", "cpu", "disk")

    assert SettingsStore.load(path, default_brightness_percent=40).current().bars == (
        "gpu",
        "cpu",
        "disk",
    )


def test_settings_store_reads_a_file_saved_before_bars_existed(tmp_path: Path) -> None:
    path = tmp_path / "display.json"
    path.write_text('{"brightness_percent": 55, "enabled": false}', encoding="utf-8")

    store = SettingsStore.load(path, default_brightness_percent=40)

    assert store.current() == DisplaySettings(55, enabled=False, bars=("battery", "cpu", "ram"))


def test_settings_store_keeps_old_value_after_invalid_update(tmp_path: Path) -> None:
    store = SettingsStore.load(tmp_path / "display.json", default_brightness_percent=40)

    with pytest.raises(ValueError):
        store.update(brightness_percent=150)

    assert store.current() == DisplaySettings(40)
    assert not (tmp_path / "display.json").exists()


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"brightness_percent": 300, "enabled": true}', '{"enabled": true}'],
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
        "bars": ("battery", "cpu", "ram"),
    }
    assert (
        handle_request(store, {"command": "set", "brightness_percent": 65})["brightness_percent"]
        == 65
    )
    assert handle_request(store, {"command": "set", "enabled": False})["enabled"] is False
    changed = handle_request(store, {"command": "set", "bars": ["swap", "cpu", "gpu"]})
    assert changed["bars"] == ("swap", "cpu", "gpu")


@pytest.mark.parametrize(
    ("request_body", "message"),
    [
        ([], "JSON object"),
        ({"command": "reboot"}, "command"),
        ({"command": "set", "color": "red"}, "unknown fields: color"),
        ({"command": "set", "brightness_percent": "max"}, "brightness_percent"),
        ({"command": "set", "enabled": "on"}, "enabled"),
        ({"command": "set", "bars": "cpu,ram,gpu"}, "bars"),
        ({"command": "set", "bars": ["cpu", "ram", "fan"]}, "bars"),
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
    assert client.set(bars=("disk", "gpu", "battery")).bars == ("disk", "gpu", "battery")
    assert store.current() == DisplaySettings(80, enabled=False, bars=("disk", "gpu", "battery"))


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
