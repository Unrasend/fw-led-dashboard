"""The settings window: what each panel shows, drawn as the panels themselves (GTK 3).

It runs as a process of its own (`--settings`, the "LED Matrix Dashboard" menu entry or the
tray's "Settings…") and exits when closed, so it takes memory only while it is open; starting
it again raises the open window.

Both modules are drawn from the frames the dashboard shows right now (the control socket's
"frames" command, a few times a second), with an outline around each widget. Click a widget
to select it and change its settings on the right; drag it up or down or onto the other module;
drag it into empty rows to place it there; drag the top or bottom edge of bars, the equalizer
or a space to resize it. Every change is applied at once and the dashboard keeps it.
"""

from __future__ import annotations

import signal
import sys
from collections.abc import Callable
from dataclasses import replace

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
from gi.repository import Gdk, GLib, Gtk, PangoCairo

from .audio import AUDIO_SOURCES
from .bar_sources import BAR_SOURCES
from .config import CPU_ALERT_MAX_CELSIUS, CPU_ALERT_MIN_CELSIUS
from .control import ControlClient, ControlError, DisplaySettings
from .frame import FRAME_HEIGHT, FRAME_WIDTH, FramePixels
from .layout import SIDES, TEMPERATURE_SOURCES, Layout, Widget, default_layout, placed
from .layout_edit import (
    ADD_LABELS,
    ADDABLE,
    can_add,
    drop_index,
    free_at,
    free_rows,
    height_limit,
    insert_widget,
    move_widget,
    other_side,
    place_widget,
    remove_widget,
    replace_widget,
    resize_from_top,
    resize_widget,
    set_separator_style,
    widget_at,
)
from .led_icon import icon_pixbuf
from .tray_model import SWAP_HINT

APP_ID = "io.github.unrasend.FwLedDashboard"
PITCH = 10  # pixels per LED
MARGIN = 16
GAP = 44  # between the two modules
LABEL_HEIGHT = 26
PANEL_WIDTH = FRAME_WIDTH * PITCH
PANEL_HEIGHT = FRAME_HEIGHT * PITCH
EDGE_GRAB = 5  # how close to a bottom edge a press starts a resize
DRAG_THRESHOLD = 4
FRAME_POLL_MS = 150
STATUS_POLL_SECONDS = 2
SLIDER_DELAY_MS = 120
NUMBER_DELAY_MS = 400
RESIZE_DELAY_MS = 150

TEMPERATURE_LABELS = {"cpu": "CPU", "gpu": "GPU", "outside": "Outside"}
TEMPERATURE_HINTS = {
    "cpu": "From the CPU's hwmon sensor (Tctl on AMD). It blinks from the value below on and "
    "stops a little lower again.",
    "gpu": "The discrete GPU when the graphics module is in, else the built-in one. A sleeping "
    "GPU is not woken up to read it: it shows a dim “--”.",
    "outside": "From the weather. The dot after the number marks it as the outside "
    "temperature.",
}
SOUND_LABELS = {"microphone": "Microphone", "system": "System sound"}
SOUND_HINT = (
    "System sound is what the laptop plays. With an equalizer on both modules it is shown "
    "in stereo: the left channel on the left module, the right channel on the right one."
)
BAR_SLOTS = ("Left bar", "Middle bar", "Right bar")
Selection = tuple[str, int]  # (layout side, index)


def _rgb(hex_color: str) -> tuple[float, float, float]:
    value = int(hex_color.lstrip("#"), 16)
    return ((value >> 16) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255)


LED_OFF = _rgb("#2b2521")
LED_ON = _rgb("#ffb347")
PANEL_BODY = _rgb("#121110")


class PanelCanvas(Gtk.DrawingArea):
    """Both modules, LED for LED, with an outline around each widget.

    Sides here are physical: "left" is the module left of the keyboard. The window maps them
    to the layout (they differ while the panels are swapped) and decides what a drag does:
    `plan(side, index, to_side, top, row)` answers ("place", top) when the widget fits there
    over empty rows, else ("insert", index, allowed).
    """

    def __init__(
        self,
        *,
        on_select: Callable[[str | None, int | None], None],
        plan: Callable[[str, int, str, int, float], tuple],
        on_drop: Callable[[str, int, str, tuple], None],
        on_resize: Callable[[str, int, str, int, bool], None],
    ) -> None:
        super().__init__()
        self._on_select = on_select
        self._plan = plan
        self._on_drop = on_drop
        self._on_resize = on_resize
        self._frames: dict[str, FramePixels] | None = None
        self._widgets: dict[str, tuple[Widget, ...]] = {side: () for side in SIDES}
        self._selected: tuple[str, int] | None = None
        self._hover: tuple[str, int] | None = None
        self._drag: dict[str, object] | None = None
        self._target: tuple[str, tuple, int] | None = None  # side, plan, height
        self.set_size_request(
            2 * MARGIN + 2 * PANEL_WIDTH + GAP, MARGIN + PANEL_HEIGHT + LABEL_HEIGHT + 4
        )
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._pressed)
        self.connect("motion-notify-event", self._moved)
        self.connect("button-release-event", self._released)
        self.connect("leave-notify-event", self._left)

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    def show_frames(self, frames: dict[str, FramePixels] | None) -> None:
        self._frames = frames
        self.queue_draw()

    def show_widgets(
        self, widgets: dict[str, tuple[Widget, ...]], selected: tuple[str, int] | None
    ) -> None:
        self._widgets = widgets
        self._selected = selected
        self.queue_draw()

    # geometry ------------------------------------------------------------------------------

    @staticmethod
    def _panel_x(side: str) -> float:
        return MARGIN + (0 if side == "left" else PANEL_WIDTH + GAP)

    def _side_at(self, x: float, *, nearest: bool = False) -> str | None:
        for side in SIDES:
            left = self._panel_x(side)
            if left - 6 <= x <= left + PANEL_WIDTH + 6:
                return side
        if not nearest:
            return None
        return "left" if x < MARGIN + PANEL_WIDTH + GAP / 2 else "right"

    @staticmethod
    def _row(y: float) -> float:
        return (y - MARGIN) / PITCH

    def _edges(self, side: str, index: int) -> tuple[float, float]:
        top = sum(widget.height for widget in self._widgets[side][:index])
        bottom = top + self._widgets[side][index].height
        return MARGIN + top * PITCH, MARGIN + bottom * PITCH

    def _hit(self, x: float, y: float) -> tuple[str, int, str | None] | None:
        """(side, index, "top" or "bottom" when on that edge of a resizable widget, else None).

        The selected widget's edges win; on a border between two widgets, the half of the
        grab zone inside a widget belongs to that widget."""
        side = self._side_at(x)
        if side is None:
            return None
        widgets = self._widgets[side]
        order = list(range(len(widgets)))
        if self._selected is not None and self._selected[0] == side:
            order.insert(0, self._selected[1])
        for index in order:
            if index >= len(widgets) or not widgets[index].resizable:
                continue
            top, bottom = self._edges(side, index)
            if abs(y - top) <= EDGE_GRAB and (index == order[0] or y >= top):
                return side, index, "top"
            if abs(y - bottom) <= EDGE_GRAB and (index == order[0] or y < bottom):
                return side, index, "bottom"
        index = widget_at(widgets, self._row(y))
        return None if index is None else (side, index, None)

    # mouse ---------------------------------------------------------------------------------

    def _pressed(self, _area: Gtk.DrawingArea, event: Gdk.EventButton) -> bool:
        if event.button != Gdk.BUTTON_PRIMARY or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        hit = self._hit(event.x, event.y)
        if hit is None:
            self._on_select(None, None)
            return True
        side, index, edge = hit
        self._on_select(side, index)
        widget = self._widgets[side][index]
        top, _bottom = self._edges(side, index)
        self._drag = {
            "mode": edge or "pending",
            "side": side,
            "index": index,
            "x": event.x,
            "y": event.y,
            "height": widget.height,
            "grab": (event.y - top) / PITCH,  # rows between the widget's top and the pointer
        }
        return True

    def _moved(self, _area: Gtk.DrawingArea, event: Gdk.EventMotion) -> bool:
        drag = self._drag
        if drag is None:
            hit = self._hit(event.x, event.y)
            hover = None if hit is None else hit[:2]
            if hover != self._hover:
                self._hover = hover
                self.queue_draw()
            self._set_cursor("ns-resize" if hit is not None and hit[2] else "default")
            return True
        side, index = drag["side"], drag["index"]
        rows = round((event.y - drag["y"]) / PITCH)
        if drag["mode"] in ("top", "bottom"):
            height = drag["height"] + (rows if drag["mode"] == "bottom" else -rows)
            self._on_resize(side, index, drag["mode"], height, False)
            return True
        if drag["mode"] == "pending":
            if abs(event.x - drag["x"]) + abs(event.y - drag["y"]) < DRAG_THRESHOLD:
                return True
            drag["mode"] = "move"
            self._set_cursor("grabbing")
        target = self._side_at(event.x, nearest=True)
        row = self._row(event.y)
        top = round(row - drag["grab"])
        plan = self._plan(side, index, target, top, row)
        self._target = (target, plan, drag["height"])
        self.queue_draw()
        return True

    def _released(self, _area: Gtk.DrawingArea, event: Gdk.EventButton) -> bool:
        drag, target = self._drag, self._target
        self._drag = self._target = None
        self._set_cursor("default")
        if drag is None:
            return False
        if drag["mode"] in ("top", "bottom"):
            rows = round((event.y - drag["y"]) / PITCH)
            height = drag["height"] + (rows if drag["mode"] == "bottom" else -rows)
            self._on_resize(drag["side"], drag["index"], drag["mode"], height, True)
        elif drag["mode"] == "move" and target is not None:
            to_side, plan, _height = target
            if plan[0] == "place" or plan[2]:
                self._on_drop(drag["side"], drag["index"], to_side, plan)
        self.queue_draw()
        return True

    def _left(self, _area: Gtk.DrawingArea, _event: Gdk.EventCrossing) -> bool:
        if self._hover is not None and self._drag is None:
            self._hover = None
            self.queue_draw()
        return False

    def _set_cursor(self, name: str) -> None:
        window = self.get_window()
        if window is not None:
            window.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))

    # drawing -------------------------------------------------------------------------------

    def _accent(self) -> tuple[float, float, float]:
        found, color = self.get_style_context().lookup_color("theme_selected_bg_color")
        return (color.red, color.green, color.blue) if found else _rgb("#3584e4")

    def _draw(self, _area: Gtk.DrawingArea, cr) -> bool:  # cr: cairo.Context
        accent = self._accent()
        text = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        for side in SIDES:
            x0 = self._panel_x(side)
            _rounded(cr, x0 - 7, MARGIN - 7, PANEL_WIDTH + 14, PANEL_HEIGHT + 14, 8)
            cr.set_source_rgb(*PANEL_BODY)
            cr.fill()
            frame = None if self._frames is None else self._frames.get(side)
            for row in range(FRAME_HEIGHT):
                for column in range(FRAME_WIDTH):
                    value = 0 if frame is None else frame[row][column]
                    if value:
                        level = 0.3 + 0.7 * value / 255
                        cr.set_source_rgb(*(channel * level for channel in LED_ON))
                    else:
                        cr.set_source_rgb(*LED_OFF)
                    cr.arc(
                        x0 + column * PITCH + PITCH / 2,
                        MARGIN + row * PITCH + PITCH / 2,
                        PITCH * 0.36,
                        0,
                        6.2832,
                    )
                    cr.fill()
            self._draw_outlines(cr, side, x0, accent)
            self._draw_target(cr, side, x0, accent)
            layout = self.create_pango_layout(f"{side.capitalize()} module")
            width, _height = layout.get_pixel_size()
            cr.move_to(x0 + (PANEL_WIDTH - width) / 2, MARGIN + PANEL_HEIGHT + 10)
            cr.set_source_rgba(text.red, text.green, text.blue, 0.7)
            PangoCairo.show_layout(cr, layout)
        return False

    def _draw_outlines(self, cr, side: str, x0: float, accent: tuple[float, float, float]) -> None:
        for index, (top, widget) in enumerate(placed(self._widgets[side])):
            y = MARGIN + top * PITCH
            height = widget.height * PITCH
            selected = self._selected == (side, index)
            hovered = self._hover == (side, index)
            cr.rectangle(x0 - 3.5, y + 0.5, PANEL_WIDTH + 7, height - 1)
            if selected:
                cr.set_source_rgb(*accent)
                cr.set_line_width(2)
                cr.set_dash([])
            else:
                cr.set_source_rgba(1, 1, 1, 0.45 if hovered else 0.14)
                cr.set_line_width(1)
                cr.set_dash([3, 3])
            cr.stroke()
            if selected and widget.resizable:
                cr.set_dash([])
                cr.set_source_rgb(*accent)
                for grip_y in (y - 2, y + height - 2):  # top and bottom handle
                    cr.rectangle(x0 + PANEL_WIDTH / 2 - 12, grip_y, 24, 4)
                cr.fill()
        cr.set_dash([])

    def _draw_target(self, cr, side: str, x0: float, accent: tuple[float, float, float]) -> None:
        """Where a dragged widget lands: its outline over empty rows, or a line between two
        widgets (red where it does not fit)."""
        if self._target is None or self._target[0] != side:
            return
        _side, plan, height = self._target
        if plan[0] == "place":
            cr.set_source_rgb(*accent)
            cr.set_line_width(2)
            cr.set_dash([4, 3])
            cr.rectangle(x0 - 3.5, MARGIN + plan[1] * PITCH + 0.5, PANEL_WIDTH + 7, height * PITCH - 1)
            cr.stroke()
            cr.set_dash([])
            return
        _kind, index, allowed = plan
        tops = [top for top, _widget in placed(self._widgets[side])]
        row = tops[index] if index < len(tops) else sum(w.height for w in self._widgets[side])
        cr.set_source_rgb(*(accent if allowed else _rgb("#e01b24")))
        cr.rectangle(x0 - 6, MARGIN + row * PITCH - 1.5, PANEL_WIDTH + 12, 3)
        cr.fill()


def _rounded(cr, x: float, y: float, width: float, height: float, radius: float) -> None:
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -1.5708, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)
    cr.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
    cr.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
    cr.close_path()


class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application, client: ControlClient) -> None:
        super().__init__(application=application, title="LED Matrix")
        self._client = client
        self._settings: DisplaySettings | None = None
        self._layout: Layout | None = None
        self._selected: Selection | None = None
        self._updating = False
        self._pending: dict[str, object] = {}
        self._send_source: int | None = None
        self._flash_message = ""
        self._inspector_source: int | None = None
        self._resize_base: Layout | None = None  # the layout when a resize drag started
        self.set_icon(icon_pixbuf("on", 64))
        self.set_resizable(False)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", self._on_destroy)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=18)
        self.add(outer)
        outer.pack_start(self._build_quick_controls(), False, False, 0)
        self._status = Gtk.Label(xalign=0)
        self._status.set_line_wrap(True)
        self._status.set_max_width_chars(70)
        self._status.get_style_context().add_class("dim-label")
        self._status.set_no_show_all(True)
        outer.pack_start(self._status, False, False, 0)
        outer.pack_start(Gtk.Separator(), False, False, 0)

        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        outer.pack_start(main, True, True, 0)
        self._canvas = PanelCanvas(
            on_select=self._canvas_selected,
            plan=self._plan_drop,
            on_drop=self._canvas_dropped,
            on_resize=self._canvas_resized,
        )
        main.pack_start(self._canvas, False, False, 0)
        main.pack_start(self._build_side_column(), True, True, 0)
        outer.show_all()

        self._frame_source = GLib.timeout_add(FRAME_POLL_MS, self._poll_frames)
        self._status_source = GLib.timeout_add_seconds(STATUS_POLL_SECONDS, self._poll_status)
        self.refresh()
        self._poll_frames()

    # building ------------------------------------------------------------------------------

    def _build_quick_controls(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label()
        title.set_markup("<b>LED Matrix</b>")
        self._power = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._power.connect("notify::active", self._on_switch, "enabled")
        self._brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self._brightness.set_digits(0)
        self._brightness.set_value_pos(Gtk.PositionType.RIGHT)
        self._brightness.set_size_request(200, -1)
        self._brightness.connect("format-value", lambda _scale, value: f"{value:.0f} %")
        self._brightness.connect("value-changed", self._on_brightness)
        self._swap = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._swap.set_tooltip_text(SWAP_HINT)
        self._swap.connect("notify::active", self._on_switch, "swap_panels")
        swap_label = Gtk.Label(label="Swap panels")
        swap_label.set_tooltip_text(SWAP_HINT)
        row.pack_start(title, False, False, 0)
        row.pack_start(self._power, False, False, 0)
        row.pack_start(Gtk.Label(label="Brightness", margin_start=14), False, False, 0)
        row.pack_start(self._brightness, True, True, 0)
        row.pack_start(swap_label, False, False, 6)
        row.pack_start(self._swap, False, False, 0)
        return row

    def _build_side_column(self) -> Gtk.Widget:
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.set_size_request(330, -1)
        column.pack_start(_caption("Add widget"), False, False, 0)
        buttons = Gtk.Grid(row_spacing=6, column_spacing=6, column_homogeneous=True)
        self._add_buttons: dict[str, Gtk.Button] = {}
        for position, kind in enumerate(ADDABLE):
            button = Gtk.Button(label=ADD_LABELS[kind])
            button.connect("clicked", self._on_add, kind)
            buttons.attach(button, position % 3, position // 3, 1, 1)
            self._add_buttons[kind] = button
        column.pack_start(buttons, False, False, 0)
        self._add_hint = _caption("")
        column.pack_start(self._add_hint, False, False, 0)

        self._inspector_frame = Gtk.Frame(margin_top=8)
        self._inspector = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self._inspector_frame.add(self._inspector)
        column.pack_start(self._inspector_frame, False, False, 0)

        column.pack_start(Gtk.Box(), True, True, 0)  # pushes the rest down
        self._free = _caption("")
        column.pack_start(self._free, False, False, 0)
        reset = Gtk.Button(label="Reset layout", halign=Gtk.Align.START)
        reset.set_tooltip_text("Go back to the layout the dashboard starts with")
        reset.connect("clicked", self._on_reset)
        column.pack_start(reset, False, False, 0)
        return column

    def _rebuild_inspector(self) -> None:
        for child in self._inspector.get_children():
            child.destroy()
        widget = self._selected_widget()
        if widget is None or self._settings is None:
            self._inspector.pack_start(
                _caption("Click a widget on a module to change it. Drag it to move it, also into "
                         "empty rows; drag its top or bottom edge to resize it."),
                False,
                False,
                0,
            )
            self._inspector.show_all()
            return
        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{GLib.markup_escape_text(widget.label)}</b>")
        self._inspector.pack_start(title, False, False, 0)
        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        rows: list[tuple[str, Gtk.Widget]] = []
        hints: list[str] = []
        if widget.kind == "weather":
            hints.append("No settings yet.")
        elif widget.kind == "temperature":
            rows.append(("Source", self._combo(
                {key: TEMPERATURE_LABELS[key] for key in TEMPERATURE_SOURCES},
                widget.source,
                self._on_temperature_source,
            )))
            if widget.source == "cpu":
                rows.append(("Blink from", self._cpu_alert_row()))
            hints.append(TEMPERATURE_HINTS[widget.source])
        elif widget.kind == "bars":
            for slot, key in enumerate(widget.bars):
                combo = self._combo(
                    {key: source.label for key, source in BAR_SOURCES.items()},
                    key,
                    lambda value, slot=slot: self._on_bar_source(slot, value),
                )
                combo.set_tooltip_text(BAR_SOURCES[key].hint)
                rows.append((BAR_SLOTS[slot], combo))
            rows.append(("Height", self._height_spin()))
        elif widget.kind == "equalizer":
            rows.append(("Sound", self._combo(
                {key: SOUND_LABELS[key] for key in AUDIO_SOURCES},
                self._settings.audio_source,
                self._on_sound_source,
            )))
            rows.append(("Height", self._height_spin()))
            hints.append(SOUND_HINT)
        elif widget.kind == "separator":
            rows.append(("Style", self._combo(
                {"line": "Line", "space": "Space"}, widget.style, self._on_separator_style
            )))
            if widget.style == "space":
                rows.append(("Height", self._height_spin()))
        for index, (name, control) in enumerate(rows):
            grid.attach(Gtk.Label(label=name, xalign=0), 0, index, 1, 1)
            control.set_hexpand(True)
            grid.attach(control, 1, index, 1, 1)
        self._inspector.pack_start(grid, False, False, 0)
        for hint in hints:
            self._inspector.pack_start(_caption(hint), False, False, 0)
        self._inspector.pack_start(self._widget_buttons(), False, False, 4)
        self._inspector.show_all()

    def _combo(self, choices: dict[str, str], active: str, on_change: Callable[[str], None]) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        for key, label in choices.items():
            combo.append(key, label)
        combo.set_active_id(active)
        combo.connect("changed", lambda box: box.get_active_id() and on_change(box.get_active_id()))
        return combo

    def _height_spin(self) -> Gtk.Widget:
        side, index = self._selected  # type: ignore[misc]
        widgets = self._layout.side(side)  # type: ignore[union-attr]
        low, high = height_limit(widgets, index)
        spin = Gtk.SpinButton.new_with_range(low, max(low, high), 1)
        spin.set_value(widgets[index].height)
        spin.set_numeric(True)
        spin.connect("value-changed", self._on_height)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(spin, False, False, 0)
        box.pack_start(Gtk.Label(label="rows"), False, False, 0)
        return box

    def _cpu_alert_row(self) -> Gtk.Widget:
        spin = Gtk.SpinButton.new_with_range(CPU_ALERT_MIN_CELSIUS, CPU_ALERT_MAX_CELSIUS, 1)
        spin.set_numeric(True)
        spin.set_value(self._settings.cpu_alert_celsius)  # type: ignore[union-attr]
        spin.connect(
            "value-changed",
            lambda spin: self._queue("cpu_alert_celsius", spin.get_value_as_int(), NUMBER_DELAY_MS),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(spin, False, False, 0)
        box.pack_start(Gtk.Label(label="°C"), False, False, 0)
        return box

    def _widget_buttons(self) -> Gtk.Widget:
        side, index = self._selected  # type: ignore[misc]
        count = len(self._layout.side(side))  # type: ignore[union-attr]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        physical = self._physical(side)
        towards = "go-next-symbolic" if physical == "left" else "go-previous-symbolic"
        for icon, tooltip, enabled, action in (
            ("go-up-symbolic", "Move up", index > 0, lambda: self._move_by(-1)),
            ("go-down-symbolic", "Move down", index < count - 1, lambda: self._move_by(1)),
            (towards, f"Move to the {other_side(physical)} module", True, self._move_across),
            ("user-trash-symbolic", "Remove (Delete)", True, self._remove_selected),
        ):
            button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            button.set_tooltip_text(tooltip)
            button.set_sensitive(enabled)
            button.connect("clicked", lambda _button, action=action: action())
            box.pack_start(button, False, False, 0)
        return box

    # state ---------------------------------------------------------------------------------

    def _physical(self, side: str) -> str:
        """Layout side → module it shows on (and back: the mapping is its own inverse)."""
        swapped = self._settings is not None and self._settings.swap_panels
        return other_side(side) if swapped else side

    def _selected_widget(self) -> Widget | None:
        if self._selected is None or self._layout is None:
            return None
        side, index = self._selected
        widgets = self._layout.side(side)
        return widgets[index] if index < len(widgets) else None

    def refresh(self) -> None:
        try:
            self.show_settings(self._client.status())
        except ControlError:
            self.show_settings(None)

    def show_settings(self, settings: DisplaySettings | None, *, error: str = "") -> None:
        if self._pending or self._canvas.dragging:
            return  # a change is on its way; its answer updates the window
        reachable = settings is not None
        if not reachable:
            self._status.set_text("The dashboard is not running.")
            self._status.set_visible(True)
        elif error:
            self._flash(error)
        elif not self._flash_message:
            self._status.set_visible(False)
        for control in (self._power, self._brightness, self._swap):
            control.set_sensitive(reachable)
        self._inspector_frame.set_sensitive(reachable)
        if settings is None:
            self._settings = None
            for button in self._add_buttons.values():
                button.set_sensitive(False)
            return
        relayout = self._settings is None or settings.layout != self._layout or (
            settings.swap_panels != self._settings.swap_panels
        ) or settings.audio_source != self._settings.audio_source or (
            settings.cpu_alert_celsius != self._settings.cpu_alert_celsius
        )
        self._settings = settings
        self._layout = settings.layout
        self._updating = True
        self._power.set_active(settings.enabled)
        self._brightness.set_value(max(1, settings.brightness_percent))
        self._swap.set_active(settings.swap_panels)
        self._updating = False
        if self._selected_widget() is None:
            self._selected = None
        if relayout:
            self._show_layout()
        else:
            self._update_add_buttons()

    def _show_layout(self) -> None:
        if self._layout is None:
            return
        widgets = {side: self._layout.side(self._physical(side)) for side in SIDES}
        selected = None
        if self._selected is not None:
            selected = (self._physical(self._selected[0]), self._selected[1])
        self._canvas.show_widgets(widgets, selected)
        self._update_add_buttons()
        # Rebuilt when idle: the edit may come from a control the rebuild destroys.
        if self._inspector_source is None:
            self._inspector_source = GLib.idle_add(self._rebuild_inspector_idle)

    def _rebuild_inspector_idle(self) -> bool:
        self._inspector_source = None
        self._rebuild_inspector()
        return False

    def _target_side(self) -> tuple[str, int]:
        """Layout side and index where an added widget goes: below the selected one, else at
        the end of the left module."""
        if self._selected is not None:
            return self._selected[0], self._selected[1] + 1
        side = self._physical("left")
        return side, len(self._layout.side(side))  # type: ignore[union-attr]

    def _update_add_buttons(self) -> None:
        if self._layout is None:
            return
        side, _index = self._target_side()
        for kind, button in self._add_buttons.items():
            reason = can_add(self._layout, side, kind)
            button.set_sensitive(reason is None)
            button.set_tooltip_text(reason or f"Add to the {self._physical(side)} module")
        where = "below the selected widget" if self._selected is not None else "at the bottom"
        self._add_hint.set_text(f"Adds to the {self._physical(side)} module, {where}.")
        rows = {side: free_rows(self._layout.side(self._physical(side))) for side in SIDES}
        self._free.set_text(f"Free rows: left module {rows['left']}, right module {rows['right']}")

    # edits ---------------------------------------------------------------------------------

    def _change_layout(self, layout: Layout, *, delay_ms: int = 0, rebuild: bool = True) -> None:
        self._layout = layout
        self._queue("layout", layout, delay_ms)
        if rebuild:
            self._show_layout()
        else:
            widgets = {side: layout.side(self._physical(side)) for side in SIDES}
            selected = None if self._selected is None else (
                self._physical(self._selected[0]), self._selected[1]
            )
            self._canvas.show_widgets(widgets, selected)
            self._update_add_buttons()

    def _try(self, edit: Callable[[], Layout], **options: object) -> None:
        try:
            layout = edit()
        except ValueError as error:
            self._flash(str(error))
            return
        self._change_layout(layout, **options)  # type: ignore[arg-type]

    def _flash(self, message: str) -> None:
        """Show why an edit did not happen, for a few seconds."""
        self._flash_message = message
        self._status.set_text(message)
        self._status.set_visible(True)
        GLib.timeout_add_seconds(5, self._hide_flash, message)

    def _hide_flash(self, message: str) -> bool:
        if self._flash_message == message:
            self._flash_message = ""
            if self._settings is not None:
                self._status.set_visible(False)
        return False

    def _canvas_selected(self, physical: str | None, index: int | None) -> None:
        self._selected = None if physical is None else (self._physical(physical), index)  # type: ignore[assignment]
        self._show_layout()

    def _plan_drop(
        self, physical: str, index: int, to_physical: str, top: int, row: float
    ) -> tuple:
        """("place", top) where the widget fits over empty rows, else ("insert", index,
        allowed) between two widgets, as the canvas shows while dragging."""
        if self._layout is None:
            return ("insert", 0, False)
        side, to_side = self._physical(physical), self._physical(to_physical)
        widget = self._layout.side(side)[index]
        top = max(0, min(FRAME_HEIGHT - widget.height, top))
        if free_at(self._layout, side, index, to_side, top):
            return ("place", top)
        target = self._layout.side(to_side)
        allowed = to_side == side or (
            free_rows(target) >= widget.height
            and not (widget.kind == "equalizer" and any(w.kind == "equalizer" for w in target))
        )
        return ("insert", drop_index(target, row), allowed)

    def _canvas_dropped(self, physical: str, index: int, to_physical: str, plan: tuple) -> None:
        side, to_side = self._physical(physical), self._physical(to_physical)
        if self._layout is None:
            return
        try:
            if plan[0] == "place":
                layout, new_index = place_widget(self._layout, side, index, to_side, plan[1])
            else:
                layout, new_index = move_widget(self._layout, side, index, to_side, plan[1])
        except ValueError as error:
            self._flash(str(error))
            return
        self._selected = (to_side, new_index)
        self._change_layout(layout)

    def _canvas_resized(
        self, physical: str, index: int, edge: str, height: int, final: bool
    ) -> None:
        """A resize drag computes every step from the layout it started with, so a space that
        appears or goes above the widget does not shift what the drag refers to."""
        if self._layout is None:
            return
        if self._resize_base is None:
            self._resize_base = self._layout
        base = self._resize_base
        side = self._physical(physical)
        if edge == "top":
            layout, new_index = resize_from_top(base, side, index, height)
        else:
            layout, new_index = resize_widget(base, side, index, height), index
        self._selected = (side, new_index)
        if final:
            self._resize_base = None
        if layout != self._layout or final:
            self._change_layout(layout, delay_ms=RESIZE_DELAY_MS, rebuild=final)

    def _edit_selected(self, change: Callable[[Widget], Widget]) -> None:
        widget = self._selected_widget()
        if widget is None or self._layout is None:
            return
        side, index = self._selected  # type: ignore[misc]
        self._try(lambda: replace_widget(self._layout, side, index, change(widget)))

    def _on_temperature_source(self, source: str) -> None:
        self._edit_selected(lambda widget: replace(widget, source=source))

    def _on_bar_source(self, slot: int, key: str) -> None:
        def change(widget: Widget) -> Widget:
            sources = list(widget.bars)
            sources[slot] = key
            return replace(widget, bars=tuple(sources))

        self._edit_selected(change)

    def _on_separator_style(self, style: str) -> None:
        if self._selected is None or self._layout is None:
            return
        side, index = self._selected
        layout, index = set_separator_style(self._layout, side, index, style)
        self._selected = (side, index)  # a space next to a space joins it
        self._change_layout(layout)

    def _on_height(self, spin: Gtk.SpinButton) -> None:
        if self._selected is None or self._layout is None:
            return
        side, index = self._selected
        layout = resize_widget(self._layout, side, index, spin.get_value_as_int())
        self._change_layout(layout, delay_ms=NUMBER_DELAY_MS, rebuild=False)

    def _on_sound_source(self, source: str) -> None:
        self._queue("audio_source", source, 0)

    def _on_add(self, _button: Gtk.Button, kind: str) -> None:
        if self._layout is None:
            return
        side, index = self._target_side()
        try:
            layout, index = insert_widget(self._layout, side, index, kind)
        except ValueError as error:
            self._flash(str(error))
            return
        self._selected = (side, index)
        self._change_layout(layout)

    def _move_by(self, step: int) -> None:
        if self._selected is None or self._layout is None:
            return
        side, index = self._selected
        target = index + step
        if not 0 <= target < len(self._layout.side(side)):
            return
        layout, index = move_widget(
            self._layout, side, index, side, target + (1 if step > 0 else 0)
        )
        self._selected = (side, index)
        self._change_layout(layout)

    def _move_across(self) -> None:
        if self._selected is None or self._layout is None:
            return
        side, index = self._selected
        to_side = other_side(side)
        to_index = len(self._layout.side(to_side))
        try:
            layout, to_index = move_widget(self._layout, side, index, to_side, to_index)
        except ValueError as error:
            self._flash(str(error))
            return
        self._selected = (to_side, to_index)
        self._change_layout(layout)

    def _remove_selected(self) -> None:
        if self._selected is None or self._layout is None:
            return
        side, index = self._selected
        layout = remove_widget(self._layout, side, index)
        self._selected = None
        self._change_layout(layout)

    def _on_reset(self, _button: Gtk.Button) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Reset the layout?",
        )
        dialog.format_secondary_text("Both modules go back to the widgets they start with.")
        dialog.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Reset", Gtk.ResponseType.OK)
        answer = dialog.run()
        dialog.destroy()
        if answer == Gtk.ResponseType.OK:
            self._selected = None
            self._change_layout(default_layout())

    # quick controls ------------------------------------------------------------------------

    def _on_switch(self, switch: Gtk.Switch, _property: object, field: str) -> None:
        if not self._updating:
            self._queue(field, switch.get_active(), 0)

    def _on_brightness(self, scale: Gtk.Scale) -> None:
        if not self._updating:
            self._queue("brightness_percent", int(scale.get_value()), SLIDER_DELAY_MS)

    # talking to the dashboard ------------------------------------------------------------------

    def _queue(self, field: str, value: object, delay_ms: int) -> None:
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
            settings = self._client.set(**changes)  # type: ignore[arg-type]
        except ControlError as error:
            try:
                current = self._client.status()
            except ControlError:
                self.show_settings(None)
                return False
            self._settings = None  # force a full redraw from what the dashboard has
            self.show_settings(current, error=str(error))
            return False
        self.show_settings(settings)
        return False  # one-shot

    def _poll_frames(self) -> bool:
        try:
            frames = self._client.frames()
        except ControlError:
            frames = None
        self._canvas.show_frames(frames)
        return True

    def _poll_status(self) -> bool:
        if not self._pending and not self._canvas.dragging:
            self.refresh()
        return True

    def _on_key(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if event.keyval == Gdk.KEY_Delete and not isinstance(self.get_focus(), Gtk.Editable):
            self._remove_selected()
            return True
        return False

    def _on_destroy(self, _window: Gtk.Window) -> None:
        GLib.source_remove(self._frame_source)
        GLib.source_remove(self._status_source)
        if self._inspector_source is not None:
            GLib.source_remove(self._inspector_source)
        if self._send_source is not None:
            GLib.source_remove(self._send_source)
            self._send()  # do not lose a change made just before closing


def _caption(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.set_line_wrap(True)
    label.set_max_width_chars(44)
    label.get_style_context().add_class("dim-label")
    return label


def run_settings(client: ControlClient | None = None) -> int:
    """Show the window (or raise the open one) and return once it is closed."""
    GLib.set_prgname(APP_ID)  # window class = the menu entry's StartupWMClass
    initialized, _argv = Gtk.init_check(sys.argv[:1])
    if not initialized:
        print("cannot open a display for the settings window", file=sys.stderr)
        return 1
    app = Gtk.Application(application_id=APP_ID)
    windows: list[SettingsWindow] = []

    def on_activate(app: Gtk.Application) -> None:
        if not windows:
            windows.append(SettingsWindow(app, client or ControlClient()))
        windows[0].present_with_time(Gtk.get_current_event_time())

    app.connect("activate", on_activate)
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit_app, app)
    return app.run([])


def _quit_app(app: Gtk.Application) -> bool:
    app.quit()
    return False
