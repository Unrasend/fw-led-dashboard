"""Every dashboard setting in an ordinary GTK window.

A window works where a tray menu cannot: on Wayland, on GNOME without a tray, and with
controls a menu cannot host (a real slider, a number field). It opens from the tray, from
the "LED Matrix Dashboard" entry in the application menu, or with --settings. Changes are
applied at once; the slider and the number field wait a moment so dragging or typing sends
one request, not dozens.
"""

from __future__ import annotations

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from .bar_sources import BAR_SLOT_NAMES, BAR_SOURCES
from .config import CPU_ALERT_MAX_CELSIUS, CPU_ALERT_MIN_CELSIUS
from .control import (
    ControlClient,
    ControlError,
    DisplaySettings,
    bars_side,
    weather_side,
)
from .led_icon import icon_rgba

APP_ICON_NAME = "framework-led-dashboard"
REFRESH_SECONDS = 5
SLIDER_DELAY_MS = 120
NUMBER_DELAY_MS = 500
SWAP_HINT = (
    "Show the bars on the right panel and the weather on the left.\n"
    "Also puts things right if the panels were detected the wrong way round."
)


def icon_pixbuf(state: str, size: int) -> GdkPixbuf.Pixbuf:
    data = GLib.Bytes.new(icon_rgba(state, size))
    return GdkPixbuf.Pixbuf.new_from_bytes(
        data, GdkPixbuf.Colorspace.RGB, True, 8, size, size, size * 4
    )


class SettingsWindow(Gtk.Window):
    def __init__(
        self,
        client: ControlClient,
        *,
        application: Gtk.Application | None = None,
        on_change: object = None,
    ) -> None:
        super().__init__(title="LED Matrix", application=application)
        self._client = client
        self._on_change = on_change  # called with the new settings, e.g. to update the tray
        self._settings: DisplaySettings | None = None
        self._updating = False
        self._pending: dict[str, object] = {}
        self._send_source: int | None = None
        self.set_resizable(False)
        self.set_icon(icon_pixbuf("on", 64))
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", self._on_destroy)

        grid = Gtk.Grid(row_spacing=10, column_spacing=18, margin=18)
        self.add(grid)
        self._status = Gtk.Label(xalign=0)
        self._status.get_style_context().add_class("dim-label")
        self._status.set_no_show_all(True)  # shown only while the dashboard is unreachable
        self._power = Gtk.Switch(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self._power.connect("notify::active", self._on_switch, "enabled")
        self._brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self._brightness.set_digits(0)
        self._brightness.set_value_pos(Gtk.PositionType.RIGHT)
        self._brightness.set_hexpand(True)
        self._brightness.set_size_request(220, -1)
        self._brightness.connect("format-value", lambda _scale, value: f"{value:.0f} %")
        self._brightness.connect("value-changed", self._on_brightness)
        self._swap = Gtk.Switch(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self._swap.set_tooltip_text(SWAP_HINT)
        self._swap.connect("notify::active", self._on_switch, "swap_panels")
        self._bars_title = Gtk.Label(xalign=0)
        self._bars = []
        for slot in range(len(BAR_SLOT_NAMES)):
            combo = Gtk.ComboBoxText()
            for key, source in BAR_SOURCES.items():
                combo.append(key, source.label)
            combo.connect("changed", self._on_bar, slot)
            self._bars.append(combo)
        self._cpu_alert = Gtk.SpinButton.new_with_range(
            CPU_ALERT_MIN_CELSIUS, CPU_ALERT_MAX_CELSIUS, 1
        )
        self._cpu_alert.set_numeric(True)
        self._cpu_alert.connect("value-changed", self._on_cpu_alert)
        self._cpu_alert_hint = Gtk.Label(xalign=0)
        self._cpu_alert_hint.get_style_context().add_class("dim-label")

        title = Gtk.Label(xalign=0)
        title.set_markup("<b>LED Matrix</b>")
        rows: list[tuple[Gtk.Widget, Gtk.Widget | None]] = [
            (self._status, None),
            (title, self._power),
            (Gtk.Label(label="Brightness", xalign=0), self._brightness),
            (Gtk.Label(label="Swap panels", xalign=0), self._swap),
            (Gtk.Separator(), None),
            (self._bars_title, None),
            *(
                (Gtk.Label(label=name, xalign=0), combo)
                for name, combo in zip(BAR_SLOT_NAMES, self._bars, strict=True)
            ),
            (Gtk.Separator(), None),
            (Gtk.Label(label="CPU alert", xalign=0), self._number_row()),
            (self._cpu_alert_hint, None),
        ]
        for index, (left, right) in enumerate(rows):
            if right is None:
                grid.attach(left, 0, index, 2, 1)
            else:
                grid.attach(left, 0, index, 1, 1)
                grid.attach(right, 1, index, 1, 1)
        grid.show_all()
        self._refresh_source = GLib.timeout_add_seconds(REFRESH_SECONDS, self._refresh_when_idle)
        self.refresh()

    def _number_row(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.END)
        box.pack_start(self._cpu_alert, False, False, 0)
        box.pack_start(Gtk.Label(label="°C"), False, False, 0)
        return box

    def refresh(self) -> None:
        try:
            self.show_settings(self._client.status())
        except ControlError:
            self.show_settings(None)

    def show_settings(self, settings: DisplaySettings | None) -> None:
        if self._pending:
            return  # a change is on its way; its answer will update the window
        self._settings = settings
        reachable = settings is not None
        self._status.set_visible(not reachable)
        self._status.set_text("The dashboard is not running.")
        for widget in (self._power, self._brightness, self._swap, *self._bars, self._cpu_alert):
            widget.set_sensitive(reachable)
        if settings is None:
            return
        self._updating = True
        self._power.set_active(settings.enabled)
        self._brightness.set_value(max(1, settings.brightness_percent))
        self._swap.set_active(settings.swap_panels)
        for combo, key in zip(self._bars, settings.bars, strict=True):
            combo.set_active_id(key)
            combo.set_tooltip_text(BAR_SOURCES[key].hint)
        self._cpu_alert.set_value(settings.cpu_alert_celsius)
        self._updating = False
        self._bars_title.set_markup(f"<b>{bars_side(settings).capitalize()} panel bars</b>")
        self._cpu_alert_hint.set_markup(
            "<small>"
            + GLib.markup_escape_text(
                f"The CPU temperature on the {weather_side(settings)} panel blinks from "
                "this value on, and stops a little lower again."
            )
            + "</small>"
        )

    def _on_switch(self, switch: Gtk.Switch, _property: object, field: str) -> None:
        if not self._updating:
            self._queue(field, switch.get_active(), delay_ms=0)

    def _on_brightness(self, scale: Gtk.Scale) -> None:
        if not self._updating:
            self._queue("brightness_percent", int(scale.get_value()), delay_ms=SLIDER_DELAY_MS)

    def _on_bar(self, combo: Gtk.ComboBoxText, slot: int) -> None:
        key = combo.get_active_id()
        if self._updating or key is None or self._settings is None:
            return
        combo.set_tooltip_text(BAR_SOURCES[key].hint)
        bars = list(self._pending.get("bars", self._settings.bars))
        bars[slot] = key
        self._queue("bars", bars, delay_ms=0)

    def _on_cpu_alert(self, spin: Gtk.SpinButton) -> None:
        if not self._updating:
            self._queue("cpu_alert_celsius", spin.get_value_as_int(), delay_ms=NUMBER_DELAY_MS)

    def _queue(self, field: str, value: object, *, delay_ms: int) -> None:
        self._pending[field] = value
        if self._send_source is not None:
            GLib.source_remove(self._send_source)
        self._send_source = GLib.timeout_add(delay_ms, self._send)

    def _send(self) -> bool:
        self._send_source = None
        changes, self._pending = self._pending, {}
        if not changes:
            return False
        try:
            settings = self._client.set(**changes)
        except ControlError:
            settings = None
        self.show_settings(settings)
        if callable(self._on_change):
            self._on_change(settings)
        return False  # one-shot

    def _refresh_when_idle(self) -> bool:
        if not self._pending:
            self.refresh()
        return True

    def _on_key(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_destroy(self, _window: Gtk.Window) -> None:
        GLib.source_remove(self._refresh_source)
        if self._send_source is not None:
            GLib.source_remove(self._send_source)
            self._send()  # do not lose a change made just before closing
