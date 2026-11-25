"""Tray icon for the running dashboard (GTK 3): the quick controls only.

`--tray` keeps an icon with on/off, brightness and "Swap panels"; everything else is in the
settings window, which runs as a process of its own (`--settings`) and is gone from memory once
closed. The icon depends on the desktop (tray_model):

- XFCE, MATE, Cinnamon on X11 (XEmbed): left click opens a GTK popup like the XFCE PulseAudio
  plugin's, with a real switch and slider in it;
- KDE Plasma, GNOME with the AppIndicator extension and other StatusNotifierItem hosts: the
  host draws a standard menu (sni, dbusmenu); left click opens the settings window, middle
  click switches the LED Matrix on or off, scrolling changes the brightness;
- no tray (GNOME without the extension): no icon; settings stay in the application menu.

The tray only talks to the dashboard through its control socket, so quitting it never touches
the panels.
"""

from __future__ import annotations

import os
import signal
import sys
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from .control import ControlClient, ControlError, DisplaySettings
from .dbusmenu import build_menu
from .led_icon import icon_pixbuf
from .tray_model import SWAP_HINT, choose_backend, tray_summary

# Gtk.StatusIcon is deprecated, but it is the GTK 3 icon that reports clicks and its position;
# XFCE's tray still shows it (XEmbed).
warnings.filterwarnings("ignore", category=DeprecationWarning, module=__name__)

TRAY_APP_ID = "io.github.unrasend.FwLedDashboard.Tray"
REFRESH_INTERVAL_SECONDS = 5
SEND_DELAY_MS = 120
SCROLL_STEP_PERCENT = 5
NO_TRAY_HINT = (
    "No tray on this desktop. Open “LED Matrix Dashboard” from the application menu; on GNOME, "
    "the “AppIndicator and KStatusNotifierItem Support” extension adds a tray."
)


def open_settings_window() -> None:
    """Start `--settings` as a process of its own: it shows the window (or raises the one that
    is open) and exits when it is closed, so the tray never holds the window in memory."""
    package_parent = str(Path(__file__).resolve().parent.parent)
    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
    paths = [package_parent, *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    launcher.setenv("PYTHONPATH", os.pathsep.join(dict.fromkeys(paths)), True)
    try:
        process = launcher.spawnv(
            [sys.executable, "-P", "-m", "framework_led_dashboard", "--settings"]
        )
    except GLib.Error as error:
        print(f"cannot open the settings window: {error.message}", file=sys.stderr)
        return
    process.wait_async(None, lambda process, result: process.wait_finish(result))  # reap it


class SwitchMenuItem(Gtk.MenuItem):
    """Title with a switch on the right; clicking toggles it and keeps the menu open."""

    def __init__(self, title: str, *, bold: bool = True) -> None:
        super().__init__()
        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        label = Gtk.Label(xalign=0)
        escaped = GLib.markup_escape_text(title)
        label.set_markup(f"<b>{escaped}</b>" if bold else escaped)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(label, True, True, 0)
        box.pack_end(self.switch, False, False, 0)
        self.add(box)

    def do_button_release_event(self, _event: Gdk.EventButton) -> bool:
        self.switch.set_active(not self.switch.get_active())
        return True  # handled here, so the menu does not close

    def do_activate(self) -> None:  # keyboard Enter / Space
        self.switch.set_active(not self.switch.get_active())


class ScaleMenuItem(Gtk.MenuItem):
    """Menu item holding a slider.

    A GTK menu keeps pointer events for itself and GtkScale ignores forwarded ones, so the item
    turns presses and drags over the slider into values itself; the GtkScale only draws them.
    """

    def __init__(self, icon_name: str, title: str) -> None:
        super().__init__()
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self.scale.set_draw_value(False)
        self.scale.set_size_request(240, -1)
        self.value_label = Gtk.Label(label="—")
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.pack_start(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU), False, False, 0
        )
        header.pack_start(Gtk.Label(label=title, xalign=0), True, True, 0)
        header.pack_end(self.value_label, False, False, 0)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.pack_start(header, False, False, 0)
        box.pack_start(self.scale, False, False, 0)
        self.add(box)
        self._dragging = False

    def do_button_press_event(self, event: Gdk.EventButton) -> bool:
        position = self._scale_position(event)
        if position is not None and event.button == Gdk.BUTTON_PRIMARY:
            self._dragging = True
            self._move_to(position[0])
        return True

    def do_motion_notify_event(self, event: Gdk.EventMotion) -> bool:
        if self._dragging:
            position = self._scale_position(event, clamp=True)
            if position is not None:
                self._move_to(position[0])
        return True

    def do_button_release_event(self, _event: Gdk.EventButton) -> bool:
        self._dragging = False
        return True  # keep the menu open after adjusting

    def do_scroll_event(self, event: Gdk.EventScroll) -> bool:
        step = {Gdk.ScrollDirection.UP: 1, Gdk.ScrollDirection.DOWN: -1}.get(event.direction, 0)
        if step:
            self.scale.set_value(self.scale.get_value() + step * SCROLL_STEP_PERCENT)
        return True

    def _scale_position(
        self, event: Gdk.EventButton | Gdk.EventMotion, *, clamp: bool = False
    ) -> tuple[int, int] | None:
        coordinates = self.translate_coordinates(self.scale, int(event.x), int(event.y))
        if coordinates is None:
            return None
        x, y = coordinates
        allocation = self.scale.get_allocation()
        inside = 0 <= x < allocation.width and 0 <= y < allocation.height
        return (x, y) if inside or clamp else None

    def _move_to(self, x: int) -> None:
        """Put the knob's centre under `x` (scale coordinates), like GTK's own slider does."""
        trough = self.scale.get_range_rect()
        knob_start, knob_end = self.scale.get_slider_range()
        knob = max(knob_end - knob_start, 1)
        travel = max(trough.width - knob, 1)
        fraction = min(1.0, max(0.0, (x - trough.x - knob / 2) / travel))
        adjustment = self.scale.get_adjustment()
        lower, upper = adjustment.get_lower(), adjustment.get_upper()
        self.scale.set_value(round(lower + fraction * (upper - lower)))


class XembedTray:
    """Gtk.StatusIcon with the GTK popup: switch, slider and "Swap panels"."""

    def __init__(
        self,
        client: ControlClient,
        *,
        open_settings: Callable[[], None],
        quit: Callable[[], None],
    ) -> None:
        self._client = client
        self._settings: DisplaySettings | None = None
        self._icon_state = "unreachable"
        self._send_source: int | None = None
        self._updating = False

        self._icon = Gtk.StatusIcon()
        self._icon.set_title("LED Matrix")
        self._icon.connect("activate", self._on_activate)
        self._icon.connect("popup-menu", self._on_popup_menu)
        self._icon.connect("size-changed", self._on_size_changed)

        self._menu = Gtk.Menu()
        self._unreachable = Gtk.MenuItem(label="Dashboard is not running")
        self._unreachable.set_sensitive(False)
        self._power = SwitchMenuItem("LED Matrix")
        self._power.switch.connect("notify::active", self._on_power_switched)
        self._brightness = ScaleMenuItem("display-brightness-symbolic", "Brightness")
        self._brightness.scale.connect("value-changed", self._on_brightness_moved)
        self._swap = SwitchMenuItem("Swap panels", bold=False)
        self._swap.set_tooltip_text(SWAP_HINT)
        self._swap.switch.connect("notify::active", self._on_swap_switched)
        settings_item = Gtk.MenuItem(label="Settings…")
        settings_item.set_tooltip_text("Widgets on each panel, their sources and sizes")
        settings_item.connect("activate", lambda _item: open_settings())
        for item in (
            self._unreachable,
            self._power,
            self._brightness,
            self._swap,
            Gtk.SeparatorMenuItem(),
            settings_item,
        ):
            self._menu.append(item)
        self._menu.show_all()
        self._menu.connect("deactivate", self._flush_brightness)

        self._quit_menu = Gtk.Menu()
        for label, action in (("Settings…", open_settings), ("Quit tray", quit)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _item, action=action: action())
            self._quit_menu.append(item)
        self._quit_menu.show_all()

        self.refresh()
        GLib.timeout_add_seconds(REFRESH_INTERVAL_SECONDS, self._refresh_when_idle)

    def refresh(self) -> None:
        try:
            self.show(self._client.status())
        except ControlError:
            self.show(None)

    def show(self, settings: DisplaySettings | None) -> None:
        self._settings = settings
        reachable = settings is not None
        self._unreachable.set_visible(not reachable)
        for item in (self._power, self._brightness, self._swap):
            item.set_sensitive(reachable)
        state, title, description = tray_summary(settings)
        self._set_icon(state, f"{title}\n{description}")
        if settings is None:
            return
        self._updating = True
        self._power.switch.set_active(settings.enabled)
        self._brightness.scale.set_value(max(1, settings.brightness_percent))
        self._swap.switch.set_active(settings.swap_panels)
        self._updating = False
        self._brightness.value_label.set_text(f"{settings.brightness_percent}%")

    def _on_activate(self, _icon: Gtk.StatusIcon) -> None:
        if self._menu.get_visible():
            self._menu.popdown()
            return
        self.refresh()
        found, _screen, area, _orientation = self._icon.get_geometry()
        if found:
            self._menu.popup_at_rect(
                Gdk.get_default_root_window(),
                area,
                Gdk.Gravity.SOUTH_WEST,
                Gdk.Gravity.NORTH_WEST,
                None,
            )
        else:
            self._menu.popup_at_pointer(None)

    def _on_popup_menu(self, _icon: Gtk.StatusIcon, _button: int, _time: int) -> None:
        self._quit_menu.popup_at_pointer(None)

    def _on_size_changed(self, _icon: Gtk.StatusIcon, size: int) -> bool:
        self._icon.set_from_pixbuf(icon_pixbuf(self._icon_state, max(size, 16)))
        return True

    def _refresh_when_idle(self) -> bool:
        if not self._menu.get_visible():
            self.refresh()
        return True  # keep the timer

    def _set_icon(self, state: str, tooltip: str) -> None:
        self._icon_state = state
        self._icon.set_from_pixbuf(icon_pixbuf(state, max(self._icon.get_size(), 16)))
        self._icon.set_tooltip_text(tooltip)

    def _apply(self, **changes: object) -> None:
        try:
            settings = self._client.set(**changes)
        except ControlError:
            settings = None
        self.show(settings)

    def _on_power_switched(self, switch: Gtk.Switch, _property: object) -> None:
        if not self._updating:
            self._apply(enabled=switch.get_active())

    def _on_swap_switched(self, switch: Gtk.Switch, _property: object) -> None:
        if not self._updating:
            self._apply(swap_panels=switch.get_active())

    def _on_brightness_moved(self, scale: Gtk.Scale) -> None:
        if self._updating:
            return
        self._brightness.value_label.set_text(f"{int(scale.get_value())}%")
        if self._send_source is not None:
            GLib.source_remove(self._send_source)
        self._send_source = GLib.timeout_add(SEND_DELAY_MS, self._send_brightness)

    def _send_brightness(self) -> bool:
        self._send_source = None
        self._apply(brightness_percent=int(self._brightness.scale.get_value()))
        return False  # one-shot

    def _flush_brightness(self, _menu: Gtk.Menu) -> None:
        if self._send_source is not None:
            GLib.source_remove(self._send_source)
            self._send_brightness()


class SniTray:
    """StatusNotifierItem icon; the host draws the menu that dbusmenu.build_menu describes."""

    def __init__(
        self,
        client: ControlClient,
        *,
        open_settings: Callable[[], None],
        quit: Callable[[], None],
    ) -> None:
        from .sni import StatusNotifierTray

        self._client = client
        self._open_settings = open_settings
        self._quit = quit
        self._settings: DisplaySettings | None = None
        self._scroll_target: int | None = None
        self._scroll_source: int | None = None
        self._tray = StatusNotifierTray(
            on_activate=open_settings,
            on_secondary_activate=self._toggle_power,
            on_scroll=self._on_scroll,
            on_menu_opening=self.refresh,
        )
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_INTERVAL_SECONDS, self._refresh_tick)

    def refresh(self) -> None:
        try:
            self.show(self._client.status())
        except ControlError:
            self.show(None)

    def show(self, settings: DisplaySettings | None) -> None:
        self._settings = settings
        state, title, description = tray_summary(settings)
        self._tray.update(
            state=state, title=title, description=description, menu=build_menu(settings, self)
        )

    # dbusmenu.MenuActions
    def set_enabled(self, enabled: bool) -> None:
        self._apply(enabled=enabled)

    def set_brightness(self, percent: int) -> None:
        self._apply(brightness_percent=percent)

    def set_swap(self, swapped: bool) -> None:
        self._apply(swap_panels=swapped)

    def open_settings(self) -> None:
        self._open_settings()

    def quit(self) -> None:
        self._quit()

    def _toggle_power(self) -> None:
        if self._settings is not None:
            self.set_enabled(not self._settings.enabled)

    def _on_scroll(self, delta: int) -> None:
        if self._settings is None:
            return
        base = self._scroll_target or self._settings.brightness_percent
        step = SCROLL_STEP_PERCENT if delta > 0 else -SCROLL_STEP_PERCENT
        self._scroll_target = max(1, min(100, base + step))
        if self._scroll_source is not None:
            GLib.source_remove(self._scroll_source)
        self._scroll_source = GLib.timeout_add(SEND_DELAY_MS, self._send_scroll)

    def _send_scroll(self) -> bool:
        target, self._scroll_target, self._scroll_source = self._scroll_target, None, None
        if target is not None:
            self._apply(brightness_percent=target)
        return False

    def _refresh_tick(self) -> bool:
        if self._scroll_source is None:
            self.refresh()
        return True

    def _apply(self, **changes: object) -> None:
        try:
            settings = self._client.set(**changes)
        except ControlError:
            settings = None
        self.show(settings)


def run_tray(
    client: ControlClient | None = None,
    *,
    env: Mapping[str, str] = os.environ,
) -> int:
    """Keep the tray icon the desktop supports until "Quit tray" (one tray per session)."""
    initialized, _argv = Gtk.init_check(sys.argv[:1])
    if not initialized:
        print("cannot open a display for the tray", file=sys.stderr)
        return 1
    from .sni import watcher_present

    backend = choose_backend(env, watcher_present=watcher_present())
    tray_class = {"xembed": XembedTray, "sni": SniTray}.get(backend)
    if tray_class is None:
        print(NO_TRAY_HINT, file=sys.stderr)
        return 0
    app = Gtk.Application(application_id=TRAY_APP_ID)
    try:
        app.register(None)
    except GLib.Error as error:
        print(f"cannot start: {error.message}", file=sys.stderr)
        return 1
    if app.get_is_remote():
        return 0  # a tray runs already
    trays: list[XembedTray | SniTray] = []

    def on_activate(_app: Gtk.Application) -> None:
        if trays:  # started again while running: show the settings, as a click would
            open_settings_window()
            return
        trays.append(
            tray_class(
                client or ControlClient(),
                open_settings=open_settings_window,
                quit=app.quit,
            )
        )
        app.hold()

    app.connect("activate", on_activate)
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit_app, app)
    return app.run([])


def _quit_app(app: Gtk.Application) -> bool:
    app.quit()
    return False
