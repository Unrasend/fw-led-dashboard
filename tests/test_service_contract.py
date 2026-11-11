from __future__ import annotations

import configparser
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "packaging" / "framework-led-dashboard.service"
TRAY_DESKTOP = ROOT / "packaging" / "framework-led-dashboard-tray.desktop"
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

    assert (
        exec_start[0] == "%h/.local/share/framework-led-dashboard/venv/bin/framework-led-dashboard"
    )
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


def test_tray_autostart_entry_is_a_valid_desktop_file(tmp_path: Path) -> None:
    validator = Path("/usr/bin/desktop-file-validate")
    if not validator.exists():
        pytest.skip("desktop-file-validate is not installed")
    rendered = tmp_path / "framework-led-dashboard-tray.desktop"
    rendered.write_text(
        TRAY_DESKTOP.read_text(encoding="utf-8").replace("@BIN@", "/usr/bin/true"),
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


def test_install_script_installs_both_packaging_files() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "packaging/framework-led-dashboard.service" in text
    assert "packaging/framework-led-dashboard-tray.desktop" in text


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


def test_uninstall_removes_service_tray_and_venv_but_keeps_settings() -> None:
    body = _function_body(INSTALL_SCRIPT.read_text(encoding="utf-8"), "uninstall")

    assert 'systemctl --user disable --now "$SERVICE"' in body
    assert 'rm -f "$UNIT_DIR/$SERVICE"' in body
    assert 'rm -f "$AUTOSTART_FILE"' in body
    assert 'rm -rf "$VENV"' in body
    for kept in ("$CONFIG_DIR", "$STATE_DIR"):
        assert not any(
            line.lstrip().startswith("rm") and kept in line for line in body.splitlines()
        )


def test_first_install_detects_both_panels_into_a_new_config() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert '--init-config "$PROJECT_DIR/config.example.toml" "$CONFIG_DIR/config.toml"' in text
    assert '--check-config "$CONFIG_DIR/config.toml"' in text
