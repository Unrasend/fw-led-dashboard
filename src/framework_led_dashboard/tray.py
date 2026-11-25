"""Tray icon and settings for the running dashboard (GTK 3).

`--tray` runs a Gtk.Application that keeps a tray icon; `--settings` opens the settings window
(in the running tray when there is one). The icon depends on the desktop (tray_model):

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

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from .bar_sources import BAR_COUNT, BAR_SLOT_NAMES, BAR_SOURCES
from .config import CPU_ALERT_MAX_CELSIUS, CPU_ALERT_MIN_CELSIUS
from .control import ControlClient, ControlError, DisplaySettings, bars_side, weather_side
from .dbusmenu import build_menu
from .settings_window import SWAP_HINT, SettingsWindow, icon_pixbuf
from .tray_model import choose_backend, tray_summary

# Gtk.StatusIcon is deprecated, but it is the GTK 3 icon that reports clicks and its position;
# XFCE's tray still shows it (XEmbed).
warnings.filterwarnings("ignore", category=DeprecationWarning, module=__name__)

APP_ID = "io.github.unrasend.FwLedDashboard"
REFRESH_INTERVAL_SECONDS = 5
SEND_DELAY_MS = 120
SCROLL_STEP_PERCENT = 5
BAR_SLOT_HINTS = (
    "Columns 0–1 of the {side} panel; 21 LEDs high = 100%.",
    "Columns 3–5 of the {side} panel; 21 LEDs high = 100%.",
    "Columns 7–8 of the {side} panel; 21 LEDs high = 100%.",
)
CPU_ALERT_HINT = "The CPU temperature blinks from this value on. Click to change it."
NO_TRAY_HINT = (
    "No tray on this desktop. Open “LED Matrix Dashboard” from the application menu; on GNOME, "
    "the “AppIndicator and KStatusNotifierItem Support” extension adds a tray."
)
Apply = Callable[..., None]


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


class BarMenuItem(Gtk.MenuItem):
    """ "Bar N: <source>" with a submenu of radio items, one per data source."""

    def __init__(self, slot: int, on_choose) -> None:
        super().__init__(label=BAR_SLOT_NAMES[slot])
        self.slot = slot
        self._on_choose = on_choose
        self._updating = False
        self._radios: dict[str, Gtk.RadioMenuItem] = {}
        submenu = Gtk.Menu()
        group = None
        for key, source in BAR_SOURCES.items():
            radio = Gtk.RadioMenuItem.new_with_label_from_widget(group, source.label)
            group = radio
            radio.set_tooltip_text(source.hint)
            radio.connect("toggled", self._toggled, key)
            submenu.append(radio)
            self._radios[key] = radio
        self.set_submenu(submenu)

    def show_source(self, key: str, *, side: str) -> None:
        self._updating = True
        self._radios[key].set_active(True)
        self._updating = False
        source = BAR_SOURCES[key]
        self.set_label(f"{BAR_SLOT_NAMES[self.slot]}: {source.label}")
        hint = BAR_SLOT_HINTS[self.slot].format(side=side)
        self.set_tooltip_text(f"{hint}\n\n{source.hint}")

    def _toggled(self, radio: Gtk.RadioMenuItem, key: str) -> None:
        if radio.get_active() and not self._updating:
            self._on_choose(self.slot, key)


class CpuAlertDialog(Gtk.Dialog):
    """Number input for the CPU temperature alert."""

    def __init__(self, *, celsius: float, side: str) -> None:
        super().__init__(title="CPU temperature alert")
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.MOUSE)
        self.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Apply", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        adjustment = Gtk.Adjustment(
            value=round(celsius),
            lower=CPU_ALERT_MIN_CELSIUS,
            upper=CPU_ALERT_MAX_CELSIUS,
            step_increment=1,
            page_increment=5,
        )
        self.spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
        self.spin.set_numeric(True)
        self.spin.set_activates_default(True)  # Enter applies

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.pack_start(
            Gtk.Label(label=f"Blink the CPU temperature on the {side} panel at", xalign=0),
            False,
            False,
            0,
        )
        row.pack_start(self.spin, False, False, 0)
        row.pack_start(Gtk.Label(label="°C"), False, False, 0)
        hint = Gtk.Label(xalign=0)
        hint.set_markup(
            "<small>"
            + GLib.markup_escape_text(
                f"From {CPU_ALERT_MIN_CELSIUS} to {CPU_ALERT_MAX_CELSIUS} °C. It stops blinking "
                "once the temperature is a little lower again (5 °C unless config.toml says "
                "otherwise)."
            )
            + "</small>"
        )
        hint.set_line_wrap(True)
        hint.set_max_width_chars(48)
        hint.get_style_context().add_class("dim-label")

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_border_width(12)
        content.pack_start(row, False, False, 0)
        content.pack_start(hint, False, False, 0)
        content.show_all()

    @property
    def celsius(self) -> int:
        self.spin.update()  # take a number that was typed but not yet confirmed
        return self.spin.get_value_as_int()


class CpuAlertPrompt:
    """One CPU alert dialog at a time, applying the typed value through `apply`."""

    def __init__(self, apply: Apply) -> None:
        self._apply = apply
        self._dialog: CpuAlertDialog | None = None

    def open(self, settings: DisplaySettings | None) -> None:
        if settings is None:
            return
        if self._dialog is None:
            self._dialog = CpuAlertDialog(
                celsius=settings.cpu_alert_celsius, side=weather_side(settings)
            )
            self._dialog.connect("response", self._on_response)
        self._dialog.present_with_time(Gtk.get_current_event_time())
        self._dialog.spin.grab_focus()  # selects the number, so typing replaces it

    def _on_response(self, dialog: CpuAlertDialog, response: int) -> None:
        if response == Gtk.ResponseType.OK:
            self._apply(cpu_alert_celsius=dialog.celsius)
        dialog.destroy()
        self._dialog = None


class XembedTray:
    """Gtk.StatusIcon with the GTK popup: switch, slider, bar submenus and the CPU alert."""

    def __init__(
        self,
        client: ControlClient,
        *,
        open_settings: Callable[[], None],
        quit: Callable[[], None],
        on_change: Callable[[DisplaySettings | None], None],
    ) -> None:
        self._client = client
        self._on_change = on_change
        self._settings: DisplaySettings | None = None
        self._icon_state = "unreachable"
        self._send_source: int | None = None
        self._updating = False
        self._alert = CpuAlertPrompt(self._apply)

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
        self._bars_title = Gtk.MenuItem(label="Left panel bars")
        self._bars_title.set_sensitive(False)
        self._bars_title.set_tooltip_text(
            "What each bar shows. Hover a source to see where its data comes from."
        )
        self._bars = [BarMenuItem(slot, self._on_bar_chosen) for slot in range(BAR_COUNT)]
        self._cpu_alert = Gtk.MenuItem(label="CPU alert…")
        self._cpu_alert.set_tooltip_text(CPU_ALERT_HINT)
        self._cpu_alert.connect("activate", lambda _item: self._alert.open(self._settings))
        settings_item = Gtk.MenuItem(label="Settings…")
        settings_item.connect("activate", lambda _item: open_settings())
        for item in (
            self._unreachable,
            self._power,
            self._brightness,
            self._swap,
            Gtk.SeparatorMenuItem(),
            self._bars_title,
            *self._bars,
            Gtk.SeparatorMenuItem(),
            self._cpu_alert,
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
        for item in (self._power, self._brightness, self._swap, *self._bars, self._cpu_alert):
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
        side = bars_side(settings)
        self._bars_title.set_label(f"{side.capitalize()} panel bars")
        for item, key in zip(self._bars, settings.bars, strict=True):
            item.show_source(key, side=side)
        self._cpu_alert.set_label(f"CPU alert: {settings.cpu_alert_celsius:g} °C…")

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
        self._on_change(settings)

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

    def _on_bar_chosen(self, slot: int, key: str) -> None:
        if self._settings is None:
            return
        bars = list(self._settings.bars)
        bars[slot] = key
        self._apply(bars=bars)


class SniTray:
    """StatusNotifierItem icon; the host draws the menu that dbusmenu.build_menu describes."""

    def __init__(
        self,
        client: ControlClient,
        *,
        open_settings: Callable[[], None],
        quit: Callable[[], None],
        on_change: Callable[[DisplaySettings | None], None],
    ) -> None:
        from .sni import StatusNotifierTray

        self._client = client
        self._open_settings = open_settings
        self._quit = quit
        self._on_change = on_change
        self._settings: DisplaySettings | None = None
        self._alert = CpuAlertPrompt(self._apply)
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

    def set_bar(self, slot: int, key: str) -> None:
        if self._settings is not None:
            bars = list(self._settings.bars)
            bars[slot] = key
            self._apply(bars=bars)

    def open_cpu_alert(self) -> None:
        self._alert.open(self._settings)

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
        self._on_change(settings)


class Gui:
    """The tray (if any) and the settings window of one Gtk.Application."""

    def __init__(self, app: Gtk.Application, client: ControlClient) -> None:
        self._app = app
        self._client = client
        self._window: SettingsWindow | None = None
        self._tray: XembedTray | SniTray | None = None

    def start_tray(self, backend: str) -> bool:
        if self._tray is not None:
            return True
        tray_class = {"xembed": XembedTray, "sni": SniTray}.get(backend)
        if tray_class is None:
            return False
        self._tray = tray_class(
            self._client,
            open_settings=self.show_settings,
            quit=self._app.quit,
            on_change=self._tray_changed,
        )
        self._app.hold()
        return True

    def show_settings(self) -> None:
        if self._window is None:
            self._window = SettingsWindow(
                self._client, application=self._app, on_change=self._window_changed
            )
            self._window.connect("destroy", self._window_closed)
        else:
            self._window.refresh()
        self._window.present_with_time(Gtk.get_current_event_time())

    def _window_changed(self, settings: DisplaySettings | None) -> None:
        if self._tray is not None:
            self._tray.show(settings)

    def _tray_changed(self, settings: DisplaySettings | None) -> None:
        if self._window is not None:
            self._window.show_settings(settings)

    def _window_closed(self, _window: SettingsWindow) -> None:
        self._window = None


def run_gui(
    mode: str,
    client: ControlClient | None = None,
    *,
    env: Mapping[str, str] = os.environ,
) -> int:
    """mode "tray": keep a tray icon; mode "settings": show the settings window."""
    GLib.set_prgname(APP_ID)  # window class = the menu entry's StartupWMClass
    initialized, _argv = Gtk.init_check(sys.argv[:1])
    if not initialized:
        print("cannot open a display for the tray or the settings window", file=sys.stderr)
        return 1
    app = Gtk.Application(application_id=APP_ID)
    app.add_action(Gio.SimpleAction.new("start-tray", None))
    try:
        app.register(None)
    except GLib.Error as error:
        print(f"cannot start: {error.message}", file=sys.stderr)
        return 1
    if app.get_is_remote():
        # Another instance runs already. Settings: run() activates it, which opens the window
        # there. Tray: that instance may be a settings window alone, so ask it for a tray.
        if mode == "settings":
            return app.run([])
        app.activate_action("start-tray", None)
        app.get_dbus_connection().flush_sync(None)
        return 0

    gui = Gui(app, client or ControlClient())
    activations = [0]

    def on_activate(_app: Gtk.Application) -> None:
        activations[0] += 1
        # A tray instance is activated once by its own start: that one opens nothing.
        if mode == "settings" or activations[0] > 1:
            gui.show_settings()

    def start_tray() -> bool:
        from .sni import watcher_present

        backend = choose_backend(env, watcher_present=watcher_present())
        if gui.start_tray(backend):
            return True
        print(NO_TRAY_HINT, file=sys.stderr)
        return False

    app.lookup_action("start-tray").connect("activate", lambda *_args: start_tray())
    app.connect("activate", on_activate)
    if mode == "tray" and not start_tray():
        return 0
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit_app, app)
    return app.run([])


def run_tray(client: ControlClient | None = None) -> int:
    return run_gui("tray", client)


def run_settings(client: ControlClient | None = None) -> int:
    return run_gui("settings", client)


def _quit_app(app: Gtk.Application) -> bool:
    app.quit()
    return False
