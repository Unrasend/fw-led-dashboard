from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PYTHON = "/usr/bin/python3"  # PyGObject comes from the distribution, not from PyPI


def _system_python_has_gtk() -> bool:
    if not Path(SYSTEM_PYTHON).exists():
        return False
    probe = 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gio, Gtk'
    probed = subprocess.run([SYSTEM_PYTHON, "-c", probe], capture_output=True, check=False)
    return probed.returncode == 0


@pytest.mark.skipif(
    shutil.which("dbus-run-session") is None or not _system_python_has_gtk(),
    reason="needs dbus-run-session and the system's PyGObject with GTK 3",
)
def test_tray_host_can_read_the_icon_and_drive_the_dashboard_through_the_menu() -> None:
    result = subprocess.run(
        ["dbus-run-session", "--", SYSTEM_PYTHON, str(ROOT / "scripts" / "sni_smoke.py")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    report_line = next(
        (line for line in reversed(result.stdout.splitlines()) if line.startswith("{")), None
    )
    assert report_line is not None, result.stdout + result.stderr
    report = json.loads(report_line)

    assert "error" not in report, report
    assert report["service"].startswith("org.kde.StatusNotifierItem-")
    assert report["tooltip"] == [
        "LED Matrix: on, brightness 40 %",
        "Equalizer: microphone",
    ]
    assert report["pixmaps"] == [[size, size, size * size * 4] for size in (22, 32, 48)]
    assert report["menu_path"] == "/MenuBar"
    assert report["labels"][:3] == ["LED Matrix", "Brightness: 40 %", "Swap panels"]
    for observed in (
        "power_after_click",
        "swap_after_click",
        "brightness_after_scroll",
        "activate_opened_settings",
        "menu_updated",
    ):
        assert report[observed] is True, observed
