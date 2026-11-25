from __future__ import annotations

import configparser
import functools
import http.server
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "packaging" / "framework-led-dashboard.service"
DOCKER_UNIT = ROOT / "packaging" / "framework-led-dashboard-docker.service"
DOCKER_WRAPPER = ROOT / "packaging" / "framework-led-dashboard-docker"
TRAY_DESKTOP = ROOT / "packaging" / "framework-led-dashboard-tray.desktop"
APP_ID = "io.github.unrasend.FwLedDashboard"
MENU_DESKTOP = ROOT / "packaging" / f"{APP_ID}.desktop"
INSTALL_SCRIPT = ROOT / "install.sh"


def _ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # systemd and desktop keys are case-sensitive
    parser.read(path, encoding="utf-8")
    return parser


@pytest.fixture(scope="module")
def unit() -> configparser.ConfigParser:
    return _ini(UNIT)


def test_unit_starts_after_the_audio_stack_the_equalizer_reads(
    unit: configparser.ConfigParser,
) -> None:
    after = unit["Unit"]["After"].split()
    assert {"pipewire.service", "pipewire-pulse.service", "pipewire-pulse.socket"} <= set(after)
    assert "pipewire-pulse.socket" in unit["Unit"]["Wants"].split()


def test_unit_runs_the_installed_dashboard_with_the_user_config(
    unit: configparser.ConfigParser,
) -> None:
    exec_start = unit["Service"]["ExecStart"].split()

    assert exec_start[0] == "%h/.local/share/framework-led-dashboard/bin/framework-led-dashboard"
    assert exec_start[1:] == ["--run", "%h/.config/framework-led-dashboard/config.toml"]


def test_unit_restarts_on_failure_with_a_bounded_growing_delay(
    unit: configparser.ConfigParser,
) -> None:
    service = unit["Service"]

    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "2s"
    assert int(service["RestartSteps"]) > 0
    assert service["RestartMaxDelaySec"] == "60s"
    # Exit code 2 is a configuration error (see __main__): restarting cannot fix it.
    assert "2" in service["RestartPreventExitStatus"].split()


def test_unit_logs_to_journald_and_leaves_time_to_blank_the_panels(
    unit: configparser.ConfigParser,
) -> None:
    service = unit["Service"]

    assert "StandardOutput" not in service  # default: journal
    assert service["SyslogIdentifier"] == "framework-led-dashboard"
    assert int(service["TimeoutStopSec"].removesuffix("s")) >= 10
    assert unit["Install"]["WantedBy"] == "default.target"


def test_tray_autostart_entry_runs_the_installed_tray() -> None:
    entry = _ini(TRAY_DESKTOP)["Desktop Entry"]

    assert entry["Type"] == "Application"
    assert entry["Exec"] == "@BIN@ --tray"  # the installer writes the absolute path
    assert entry["Terminal"] == "false"


def test_menu_entry_opens_the_settings_of_the_installed_program() -> None:
    entry = _ini(MENU_DESKTOP)["Desktop Entry"]

    assert entry["Exec"] == "@BIN@ --settings"
    assert entry["Icon"] == "framework-led-dashboard"
    # GNOME on Wayland matches windows to menu entries by application id.
    assert entry["StartupWMClass"] == APP_ID
    window = (ROOT / "src/framework_led_dashboard/settings_window.py").read_text()
    assert f'APP_ID = "{APP_ID}"' in window
    assert _ini(TRAY_DESKTOP)["Desktop Entry"]["Icon"] == "framework-led-dashboard"


@pytest.mark.parametrize("desktop_file", [TRAY_DESKTOP, MENU_DESKTOP], ids=lambda path: path.name)
def test_desktop_entries_are_valid(tmp_path: Path, desktop_file: Path) -> None:
    validator = Path("/usr/bin/desktop-file-validate")
    if not validator.exists():
        pytest.skip("desktop-file-validate is not installed")
    rendered = tmp_path / desktop_file.name
    rendered.write_text(
        desktop_file.read_text(encoding="utf-8").replace("@BIN@", "/usr/bin/true"),
        encoding="utf-8",
    )

    result = subprocess.run([validator, rendered], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr


def test_package_installs_the_command_the_unit_runs() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"] == {
        "framework-led-dashboard": "framework_led_dashboard.__main__:main"
    }


def test_install_script_is_strict_bash_with_valid_syntax() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert INSTALL_SCRIPT.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", INSTALL_SCRIPT], check=True)


def test_install_script_disables_the_old_visualizer_but_keeps_it_for_rollback() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "systemctl --user disable --now framework-matrix.service" in text
    for kept in ("framework-matrix.service", "start-matrix.sh"):
        for line in text.splitlines():
            assert not (line.lstrip().startswith("rm ") and kept in line), line


def test_install_script_installs_every_packaging_file() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    for name in (
        "framework-led-dashboard.service",
        "framework-led-dashboard-tray.desktop",
        "$APP_ID.desktop",
        "framework-led-dashboard.svg",
    ):
        assert f"packaging/{name}" in text
    assert f"\nAPP_ID={APP_ID}\n" in text
    for path in (ROOT / "packaging").iterdir():
        name = path.name.replace(APP_ID, "$APP_ID")
        assert f"packaging/{name}" in text, f"{path.name} is never installed"


def _function_body(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    return text[start : text.index("\n}\n", start)]


def test_install_script_help_and_unknown_options_do_nothing() -> None:
    shown = subprocess.run(
        [INSTALL_SCRIPT, "--help"], capture_output=True, text=True, check=True
    ).stdout
    assert "--uninstall" in shown

    rejected = subprocess.run(
        [INSTALL_SCRIPT, "--frobnicate"], capture_output=True, text=True, check=False
    )
    assert rejected.returncode == 1
    assert "unknown option" in rejected.stderr


def test_uninstall_removes_service_tray_menu_and_program_but_keeps_settings() -> None:
    body = _function_body(INSTALL_SCRIPT.read_text(encoding="utf-8"), "uninstall")

    assert 'systemctl --user disable --now "$SERVICE"' in body
    assert 'rm -f "$UNIT_DIR/$SERVICE"' in body
    assert 'rm -f "$AUTOSTART_FILE" "$MENU_FILE" "$ICON_FILE"' in body
    # The program, the unpacked release and the venv of versions before 1.3.
    assert 'rm -rf "$LIB_DIR" "$BIN_DIR" "$SRC_DIR" "$OLD_VENV"' in body
    for kept in ("$CONFIG_DIR", "$STATE_DIR"):
        assert not any(
            line.lstrip().startswith("rm") and kept in line for line in body.splitlines()
        )


def test_updates_stop_the_running_tray_whichever_version_started_it() -> None:
    body = _function_body(INSTALL_SCRIPT.read_text(encoding="utf-8"), "stop_tray")

    patterns = re.findall(r"""pkill -f ('[^']*'|"[^"]*")""", body)
    current, old = (pattern[1:-1].replace("\\$", "$") for pattern in patterns)
    old = old.replace("$OLD_VENV", "/home/u/.local/share/framework-led-dashboard/venv")

    # The trays of this version and of the venv-based ones before it...
    assert re.search(current, "/usr/bin/python3 -P -m framework_led_dashboard --tray")
    assert re.search(
        old,
        "/home/u/.local/share/framework-led-dashboard/venv/bin/python "
        "/home/u/.local/share/framework-led-dashboard/venv/bin/framework-led-dashboard --tray",
    )
    # ...but not the daemon, nor a shell or editor whose command line mentions the tray.
    for other in (
        "/usr/bin/python3 -P -m framework_led_dashboard --run /home/u/config.toml",
        "/bin/bash -c pgrep -f 'python3 -P -m framework_led_dashboard --tray'",
        "vim notes-about-framework_led_dashboard --tray",
    ):
        assert not re.search(current, other) and not re.search(old, other), other


def test_first_install_detects_both_panels_into_a_new_config() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert '--init-config "$PROJECT_DIR/config.example.toml" "$CONFIG_DIR/config.toml"' in text
    assert '--check-config "$CONFIG_DIR/config.toml"' in text


def test_install_script_downloads_the_release_of_the_package_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert f"\nVERSION={project['project']['version']}\n" in text


def test_install_script_runs_nothing_until_it_is_read_to_the_end() -> None:
    # Piped from the web, bash executes commands as they arrive: a cut download must not run
    # half a script, so everything sits in functions called from one block at the end (bash
    # reads a whole if-block before running it). Sourced, as the tests below do, it runs nothing.
    lines = [line for line in INSTALL_SCRIPT.read_text(encoding="utf-8").splitlines() if line]

    assert lines[-3:] == [
        'if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "$0" ]]; then',
        '    main "$@"',
        "fi",
    ]
    assert sum(line.strip() == 'main "$@"' for line in lines) == 1


def _piped(
    *args: str, home: Path, archive_url: str = "http://127.0.0.1:9/none"
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "LED_DASHBOARD_ARCHIVE_URL": archive_url}
    return subprocess.run(
        ["bash", "-s", "--", *args],
        input=INSTALL_SCRIPT.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def test_piped_help_and_unknown_options_need_no_download(tmp_path: Path) -> None:
    shown = _piped("--help", home=tmp_path)
    assert shown.returncode == 0
    assert "| bash -s -- --uninstall" in shown.stdout

    rejected = _piped("--frobnicate", home=tmp_path)
    assert rejected.returncode == 1
    assert "unknown option" in rejected.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def release_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    served = tmp_path / "served"
    served.mkdir()
    handler = functools.partial(_QuietHandler, directory=str(served))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", served
    finally:
        server.shutdown()
        server.server_close()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def _stub_release(path: Path) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, text in {
            "install.sh": 'echo "stub install from $(dirname "$0") args=[$*]"\n',
            "packaging/framework-led-dashboard.service": "[Unit]\n",
        }.items():
            data = text.encode()
            info = tarfile.TarInfo(f"framework-led-dashboard-9.9.9/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_piped_install_unpacks_the_release_and_runs_its_installer(
    tmp_path: Path, release_server: tuple[str, Path]
) -> None:
    base_url, served = release_server
    _stub_release(served / "release.tar.gz")
    home = tmp_path / "home"
    src = home / ".local/share/framework-led-dashboard/src"
    src.mkdir(parents=True)
    (src / "left-over-from-an-older-release").touch()

    result = _piped(home=home, archive_url=f"{base_url}/release.tar.gz")

    assert result.returncode == 0, result.stderr
    assert f"stub install from {src} args=[]" in result.stdout
    assert sorted(path.name for path in src.iterdir()) == ["install.sh", "packaging"]
    assert sorted(path.name for path in src.parent.iterdir()) == ["src"]  # no temp files


def test_piped_install_stops_cleanly_when_the_release_cannot_be_downloaded(
    tmp_path: Path, release_server: tuple[str, Path]
) -> None:
    base_url, _served = release_server

    result = _piped(home=tmp_path, archive_url=f"{base_url}/missing.tar.gz")

    assert result.returncode == 1
    assert "could not download" in result.stderr
    assert list((tmp_path / ".local/share/framework-led-dashboard").iterdir()) == []


def _sourced(script: str, *, tmp_path: Path, path_tools: dict[str, str], **env: str) -> str:
    """Run `script` after sourcing install.sh, with only `path_tools` on PATH."""
    tools = tmp_path / "bin"
    tools.mkdir(parents=True, exist_ok=True)
    for name in ("dirname",):
        (tools / name).symlink_to(shutil.which(name))
    for name, body in path_tools.items():
        (tools / name).write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        (tools / name).chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-c", f'source "{INSTALL_SCRIPT}"; {script}'],
        capture_output=True,
        text=True,
        check=True,
        env={"HOME": str(tmp_path), "PATH": str(tools), **env},
        timeout=30,
    )
    return result.stdout + result.stderr


NO_GTK_PYTHON = "exit 1"  # stands in for python3 without PyGObject


@pytest.mark.parametrize("manager", ["pacman", "apt", "dnf", "zypper"])
def test_every_package_manager_has_names_for_every_need(manager: str, tmp_path: Path) -> None:
    script = 'for need in gtk audio gnome-tray; do echo "$need=$(packages_for MANAGER $need)"; done'
    names = _sourced(
        script.replace("MANAGER", manager), tmp_path=tmp_path, path_tools={}
    ).splitlines()

    assert all(line.split("=", 1)[1] for line in names), names
    if manager == "apt":
        assert names[0] == "gtk=python3-gi python3-gi-cairo gir1.2-gtk-3.0"


def test_missing_needs_finds_gtk_audio_and_gnomes_missing_tray(tmp_path: Path) -> None:
    fake_python = tmp_path / "python3"
    fake_python.write_text(f"#!/bin/bash\n{NO_GTK_PYTHON}\n", encoding="utf-8")
    fake_python.chmod(0o755)

    on_gnome = _sourced(
        "missing_needs",
        tmp_path=tmp_path,
        path_tools={"busctl": "exit 1"},  # no StatusNotifierWatcher on the bus
        PYTHON=str(fake_python),
        XDG_CURRENT_DESKTOP="ubuntu:GNOME",
    )
    assert on_gnome.split() == ["gtk", "audio", "gnome-tray"]

    with_pw_record_on_kde = _sourced(
        "missing_needs",
        tmp_path=tmp_path / "kde",
        path_tools={"pw-record": "exit 0", "busctl": "exit 1"},
        PYTHON=str(fake_python),
        XDG_CURRENT_DESKTOP="KDE",
    )
    assert with_pw_record_on_kde.split() == ["gtk"]


def test_declined_packages_are_named_with_the_command_but_not_installed(tmp_path: Path) -> None:
    fake_python = tmp_path / "python3"
    fake_python.write_text(f"#!/bin/bash\n{NO_GTK_PYTHON}\n", encoding="utf-8")
    fake_python.chmod(0o755)
    sudo_log = tmp_path / "sudo.log"

    output = _sourced(
        "SKIP_PACKAGES=1; install_system_packages",
        tmp_path=tmp_path,
        path_tools={"pacman": "exit 0", "sudo": f'echo "$*" >> "{sudo_log}"'},
        PYTHON=str(fake_python),
    )

    assert "PyGObject with cairo and GTK 3" in output
    assert "sudo pacman -S --needed python-gobject python-cairo gtk3 libpulse" in output
    assert not sudo_log.exists()


def test_yes_installs_the_missing_packages_with_the_package_manager(tmp_path: Path) -> None:
    fake_python = tmp_path / "python3"
    fake_python.write_text(f"#!/bin/bash\n{NO_GTK_PYTHON}\n", encoding="utf-8")
    fake_python.chmod(0o755)
    sudo_log = tmp_path / "sudo.log"

    _sourced(
        "ASSUME_YES=1; install_system_packages",
        tmp_path=tmp_path,
        path_tools={
            "apt-get": "exit 0",
            "pw-record": "exit 0",
            "sudo": f'echo "$*" >> "{sudo_log}"',
        },
        PYTHON=str(fake_python),
    )

    assert sudo_log.read_text(encoding="utf-8").split("\n")[0] == (
        "apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0"
    )


def test_immutable_fedora_only_gets_the_command_to_run(tmp_path: Path) -> None:
    output = _sourced(
        'install_command rpm-ostree $(packages_for rpm-ostree gtk)',
        tmp_path=tmp_path,
        path_tools={},
    )

    assert output.strip() == "rpm-ostree install python3-gobject gtk3 (then reboot)"


def test_docker_unit_runs_the_wrapper_with_the_plain_units_restart_rules(
    unit: configparser.ConfigParser,
) -> None:
    docker = _ini(DOCKER_UNIT)
    wrapper = "%h/.local/share/framework-led-dashboard/bin/framework-led-dashboard-docker"

    assert docker["Service"]["ExecStart"] == wrapper
    assert docker["Service"]["ExecStopPost"] == f"{wrapper} --remove"
    assert docker["Install"]["WantedBy"] == "default.target"  # with the session, not at boot
    for key in ("Restart", "RestartSec", "RestartSteps", "RestartMaxDelaySec",
                "RestartPreventExitStatus", "TimeoutStopSec", "SyslogIdentifier"):
        assert docker["Service"][key] == unit["Service"][key], key
    assert docker["Unit"]["After"] == unit["Unit"]["After"]


def _docker_command(tmp_path: Path, **env: str) -> list[str]:
    script = tmp_path / "wrapper"
    script.write_text(DOCKER_WRAPPER.read_text(encoding="utf-8").replace("@VERSION@", "9.9"))
    runtime = tmp_path / "run"
    (runtime / "pulse").mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["/bin/bash", str(script), "--print"],
        capture_output=True,
        text=True,
        check=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ["PATH"],
            "XDG_RUNTIME_DIR": str(runtime),
            "TZ": "Europe/Prague",
            **env,
        },
        timeout=30,
    )
    return subprocess.run(
        ["/bin/bash", "-c", f"printf '%s\\n' {result.stdout}"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()


def test_docker_wrapper_gives_the_container_only_what_the_daemon_needs(tmp_path: Path) -> None:
    command = _docker_command(tmp_path)
    runtime = tmp_path / "run"
    text = " ".join(command)

    assert command[:2] == ["docker", "run"]
    assert command[-3:] == ["framework-led-dashboard:9.9", "--run", "/config/config.toml"]
    for flag in ("--read-only", "--init", "--rm"):
        assert flag in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    # The serial ports and nothing else: /dev without its submounts (terminals, shared memory)
    # and a device rule for ttyACM only.
    assert "--mount=type=bind,src=/dev,dst=/dev,readonly,bind-recursive=disabled" in command
    assert command[command.index("--device-cgroup-rule") + 1] == "c 166:* rmw"
    # Only its own folder of the session's runtime directory, never all of it.
    assert f"--mount=type=bind,src={runtime}/framework-led-dashboard," in text
    assert f"src={runtime}," not in text
    assert "--mount=type=bind,src=" + str(tmp_path / "home/.config/framework-led-dashboard") + ",dst=/config,readonly" in command
    assert "TZ=Europe/Prague" in command
    # A user-defined network of its own: isolated, and its DNS asks the desktop's resolver.
    assert command[command.index("--network") + 1] == "framework-led-dashboard"
    for flag in ("--privileged", "--pid", "--ipc", "--cap-add", "--device"):
        assert not any(part == flag or part.startswith(f"{flag}=") for part in command), flag
    assert "docker.sock" not in text


def test_docker_wrapper_passes_the_sound_sockets_that_exist(tmp_path: Path) -> None:
    import socket as socket_module

    runtime = tmp_path / "run"
    (runtime / "pulse").mkdir(parents=True)
    with socket_module.socket(socket_module.AF_UNIX) as pipewire:
        pipewire.bind(str(runtime / "pipewire-0"))
        command = _docker_command(tmp_path)

    inside = f"/run/user/{os.getuid()}"
    assert f"--mount=type=bind,src={runtime}/pipewire-0,dst={inside}/pipewire-0" in command
    assert not any("pulse/native" in part for part in command)  # not there: not passed
    assert f"XDG_RUNTIME_DIR={inside}" in command


def test_docker_wrapper_can_use_another_network_when_asked(tmp_path: Path) -> None:
    command = _docker_command(tmp_path, LED_DASHBOARD_DOCKER_NETWORK="host")

    assert command[command.index("--network") + 1] == "host"


def test_docker_wrapper_rejects_unknown_options() -> None:
    result = subprocess.run(
        ["/bin/bash", str(DOCKER_WRAPPER), "--bogus"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2 and "usage" in result.stderr


def test_dockerfile_builds_a_small_image_that_runs_unprivileged() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(r"^FROM alpine:3\.\d+$", dockerfile, re.MULTILINE)  # a pinned release
    assert not re.search(r"\bpip3? install", dockerfile)  # standard library only
    assert "USER 65534:65534" in dockerfile
    assert 'ENTRYPOINT ["python3", "-P", "-m", "framework_led_dashboard"]' in dockerfile
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert ignored[ignored.index("*"):] == ["*", "!src/framework_led_dashboard", "**/__pycache__"]


@pytest.mark.parametrize(("unit_text", "mode"), [
    ("ExecStart=%h/.local/share/framework-led-dashboard/bin/framework-led-dashboard-docker", "docker"),
    ("ExecStart=%h/.local/share/framework-led-dashboard/bin/framework-led-dashboard --run x", "native"),
    (None, "native"),
])
def test_an_update_keeps_the_installed_variant(tmp_path: Path, unit_text: str | None, mode: str) -> None:
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    if unit_text is not None:
        (unit_dir / "framework-led-dashboard.service").write_text(unit_text, encoding="utf-8")

    output = _sourced("installed_mode", tmp_path=tmp_path, path_tools={"grep": 'exec /usr/bin/grep "$@"'})

    assert output.strip() == mode


def test_docker_mode_does_not_ask_for_audio_tools_the_container_brings(tmp_path: Path) -> None:
    fake_python = tmp_path / "python3"
    fake_python.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)

    output = _sourced(
        "MODE=docker; missing_needs",
        tmp_path=tmp_path,
        path_tools={"busctl": "exit 0"},
        PYTHON=str(fake_python),
        XDG_CURRENT_DESKTOP="XFCE",
    )

    assert output.split() == []


def test_install_script_offers_docker_and_native_and_removes_the_image_on_uninstall() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "--docker) MODE=docker ;;" in text and "--native) MODE=native ;;" in text
    assert "remove_docker_images" in _function_body(text, "uninstall")
    assert "docker network rm framework-led-dashboard" in _function_body(text, "uninstall")
    assert 'docker build --network host --tag "$DOCKER_IMAGE:$VERSION"' in text
