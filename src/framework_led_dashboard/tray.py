"""System tray control for the running dashboard (GTK 3).

Left click opens a native GTK menu styled by the desktop theme, like the XFCE PulseAudio
plugin's popup: an On/Off switch, a brightness slider, a switch that swaps what the two panels
show, and one submenu per bar to choose its data source (each source has a tooltip saying
where the data comes from).
Right click offers Quit. The tray only talks to the dashboard through its control socket, so
quitting the tray never touches the panels.
"""

from __future__ import annotations

import math
import signal
import sys
import warnings

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from .bar_sources import BAR_COUNT, BAR_SOURCES
from .control import ControlClient, ControlError, DisplaySettings, bars_side

# Gtk.StatusIcon is deprecated, but it is the GTK 3 icon that reports clicks and its position;
# XFCE's tray still shows it (XEmbed).
warnings.filterwarnings("ignore", category=DeprecationWarning, module=__name__)

REFRESH_INTERVAL_SECONDS = 5
SEND_DELAY_MS = 120
SCROLL_STEP_PERCENT = 5
BAR_SLOT_NAMES = ("Bar 1 (left)", "Bar 2 (middle)", "Bar 3 (right)")
BAR_SLOT_HINTS = (
    "Columns 0–1 of the {side} panel; 21 LEDs high = 100%.",
    "Columns 3–5 of the {side} panel; 21 LEDs high = 100%.",
    "Columns 7–8 of the {side} panel; 21 LEDs high = 100%.",
)
SWAP_HINT = (
    "Show the bars on the right panel and the weather on the left.\n"
    "Also puts things right if the panels were detected the wrong way round."
)
ICON_BAR_HEIGHTS = (3, 6, 4)
ICON_DOT_ROWS = 7


def led_icon(state: str, size: int) -> GdkPixbuf.Pixbuf:
    """LED-matrix glyph: amber equalizer when on, grey when off, red dot when unreachable."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    context = cairo.Context(surface)
    context.scale(size / 64, size / 64)
    _rounded_rectangle(context, 12, 2, 40, 60, 9)
    context.set_source_rgb(0.15, 0.15, 0.15)
    context.fill()
    lit = (1.0, 0.69, 0.23) if state == "on" else (0.6, 0.6, 0.6)
    unlit = (0.30, 0.23, 0.13) if state == "on" else (0.27, 0.27, 0.27)
    for column, height in enumerate(ICON_BAR_HEIGHTS):
        for row in range(ICON_DOT_ROWS):
            context.set_source_rgb(*(lit if row >= ICON_DOT_ROWS - height else unlit))
            center_y = 10.5 + row * 7 + row // 4
            context.arc(21.5 + column * 10, center_y, 3.5, 0, 2 * math.pi)
            context.fill()
    if state == "unreachable":
        context.set_source_rgb(0.9, 0.28, 0.3)
        context.arc(48, 48, 12, 0, 2 * math.pi)
        context.fill()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)


def _rounded_rectangle(
    context: cairo.Context, x: float, y: float, width: float, height: float, radius: float
) -> None:
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()


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


class TrayController:
    def __init__(self, client: ControlClient) -> None:
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
        self._bars_title = Gtk.MenuItem(label="Left panel bars")
        self._bars_title.set_sensitive(False)
        self._bars_title.set_tooltip_text(
            "What each bar shows. Hover a source to see where its data comes from."
        )
        self._bars = [BarMenuItem(slot, self._on_bar_chosen) for slot in range(BAR_COUNT)]
        for item in (
            self._unreachable,
            self._power,
            self._brightness,
            self._swap,
            Gtk.SeparatorMenuItem(),
            self._bars_title,
            *self._bars,
        ):
            self._menu.append(item)
        self._menu.show_all()
        self._menu.connect("deactivate", self._flush_brightness)

        self._quit_menu = Gtk.Menu()
        quit_item = Gtk.MenuItem(label="Quit tray")
        quit_item.connect("activate", lambda _item: Gtk.main_quit())
        self._quit_menu.append(quit_item)
        self._quit_menu.show_all()

        self._refresh()
        GLib.timeout_add_seconds(REFRESH_INTERVAL_SECONDS, self._refresh_when_idle)

    def _on_activate(self, _icon: Gtk.StatusIcon) -> None:
        if self._menu.get_visible():
            self._menu.popdown()
            return
        self._refresh()
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
        self._icon.set_from_pixbuf(led_icon(self._icon_state, max(size, 16)))
        return True

    def _refresh_when_idle(self) -> bool:
        if not self._menu.get_visible():
            self._refresh()
        return True  # keep the timer

    def _refresh(self) -> None:
        try:
            self._show(self._client.status())
        except ControlError:
            self._show(None)

    def _show(self, settings: DisplaySettings | None) -> None:
        self._settings = settings
        reachable = settings is not None
        self._unreachable.set_visible(not reachable)
        for item in (self._power, self._brightness, self._swap, *self._bars):
            item.set_sensitive(reachable)
        if settings is None:
            self._set_icon("unreachable", "LED Matrix: dashboard is not running")
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
        state = "on" if settings.enabled else "off"
        sources = ", ".join(BAR_SOURCES[key].label for key in settings.bars)
        self._set_icon(
            state,
            f"LED Matrix: {state}, brightness {settings.brightness_percent}%\n"
            f"Bars on the {side} panel: {sources}",
        )

    def _set_icon(self, state: str, tooltip: str) -> None:
        self._icon_state = state
        self._icon.set_from_pixbuf(led_icon(state, max(self._icon.get_size(), 16)))
        self._icon.set_tooltip_text(tooltip)

    def _apply(self, **changes: object) -> None:
        try:
            self._show(self._client.set(**changes))
        except ControlError:
            self._show(None)

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


def run_tray(client: ControlClient | None = None) -> int:
    initialized, _argv = Gtk.init_check(sys.argv[:1])
    if not initialized:
        print("cannot open a display for the tray icon", file=sys.stderr)
        return 1
    controller = TrayController(client or ControlClient())
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit)
    Gtk.main()
    del controller
    return 0


def _quit() -> bool:
    Gtk.main_quit()
    return False
