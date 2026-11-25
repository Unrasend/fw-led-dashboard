"""Render docs/settings-window.png: the settings window with made-up data, off screen.

    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 PYTHONPATH=src python3 scripts/build_settings_screenshot.py

(any GTK display works; broadwayd keeps it off your desktop). The window talks to a control
socket of its own here, served with the real renderers, so it shows what it would on a laptop.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk

from framework_led_dashboard.app import DashboardSnapshot, render_side
from framework_led_dashboard.audio import Spectrum
from framework_led_dashboard.control import (
    ControlClient,
    ControlServer,
    DisplaySettings,
    SettingsStore,
)
from framework_led_dashboard.settings_window import SettingsWindow
from framework_led_dashboard.system_metrics import SystemMetrics
from framework_led_dashboard.weather import WeatherReading

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "settings-window.png"
SCALE = 2  # crisp on high-density screens
SNAPSHOT = DashboardSnapshot(
    metrics=SystemMetrics(battery_percent=78, cpu_percent=35, ram_used_percent=58),
    cpu_temperature_c=67,
    gpu_temperature_c=58,
    weather=WeatherReading(7.0, 2, True),
)
SPECTRUM = Spectrum(
    (0.92, 0.8, 0.62, 0.7, 0.48, 0.55, 0.36, 0.24, 0.14),
    (0.86, 0.84, 0.55, 0.62, 0.52, 0.4, 0.3, 0.2, 0.1),
)


def main() -> None:
    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    folder = Path(tempfile.mkdtemp(prefix="fwled-"))
    socket_path = folder / "control.sock"
    store = SettingsStore(DisplaySettings(40))
    server = ControlServer(
        store,
        path=socket_path,
        frames=lambda: {
            side: render_side(side, store.current(), SNAPSHOT, SPECTRUM, 0.2)
            for side in ("left", "right")
        },
    )
    server.open()
    threading.Thread(target=lambda: [server.step() for _ in iter(int, 1)], daemon=True).start()

    app = Gtk.Application(
        application_id="io.github.unrasend.FwLedDashboard.Screenshot",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )

    def capture(window: SettingsWindow) -> bool:
        width, height = window.get_allocated_width(), window.get_allocated_height()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width * SCALE, height * SCALE)
        context = cairo.Context(surface)
        context.scale(SCALE, SCALE)
        window.draw(context)
        surface.write_to_png(str(OUTPUT))
        print(f"wrote {OUTPUT}: {width * SCALE}x{height * SCALE}")
        window.destroy()
        app.quit()
        return False

    def activate(app: Gtk.Application) -> None:
        window = SettingsWindow(app, ControlClient(socket_path))
        window.present()
        window._canvas_selected("left", 2)  # the bars: sources, blink rules and height
        GLib.timeout_add(1500, capture, window)

    app.connect("activate", activate)
    app.run([])
    server.close()
    os.rmdir(folder)


if __name__ == "__main__":
    main()
