"""A StatusNotifierItem tray icon with a com.canonical.dbusmenu menu, over D-Bus with Gio.

This is the tray protocol of KDE Plasma (X11 and Wayland), of GNOME with the AppIndicator
extension (on by default in Ubuntu), Cinnamon, Budgie, XFCE's status tray and Waybar. The
host draws the icon and the menu; this side only answers on the session bus:

- the item at /StatusNotifierItem: icon pixmaps, tooltip, and Activate (left click on KDE),
  SecondaryActivate (middle click) and Scroll;
- the menu at /MenuBar, built from a dbusmenu.MenuItem tree;
- registration with org.kde.StatusNotifierWatcher, repeated whenever the watcher comes back
  (a panel restart).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from .dbusmenu import MenuItem, layout
from .led_icon import icon_argb32
from .scheduler import LOGGER

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"
MENU_INTERFACE = "com.canonical.dbusmenu"
MENU_PATH = "/MenuBar"
ICON_SIZES = (22, 32, 48)

_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate">
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
    </method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s"/></signal>
  </interface>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
      <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" direction="in"/><arg type="as" direction="in"/>
      <arg type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" direction="in"/><arg type="s" direction="in"/>
      <arg type="v" direction="in"/><arg type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" direction="in"/><arg type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" direction="in"/>
      <arg type="ai" direction="out"/><arg type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})"/><arg type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
    <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
  </interface>
</node>
"""


def watcher_present(bus: Gio.DBusConnection | None = None) -> bool:
    """Whether a tray host on the session bus takes StatusNotifierItems."""
    try:
        bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (WATCHER_NAME,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
    except GLib.Error:
        return False
    return bool(reply.unpack()[0])


def _property_variants(props: dict[str, tuple[str, object]]) -> dict[str, GLib.Variant]:
    return {name: GLib.Variant(kind, value) for name, (kind, value) in props.items()}


def _layout_variant(node: tuple) -> GLib.Variant:
    item_id, props, children = node
    return GLib.Variant(
        "(ia{sv}av)",
        (item_id, _property_variants(props), [_layout_variant(child) for child in children]),
    )


class StatusNotifierTray:
    """One tray icon. Call update() whenever the icon state, tooltip or menu changes."""

    def __init__(
        self,
        *,
        on_activate: Callable[[], None],
        on_secondary_activate: Callable[[], None],
        on_scroll: Callable[[int], None],
        on_menu_opening: Callable[[], None],
        bus: Gio.DBusConnection | None = None,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._on_activate = on_activate
        self._on_secondary_activate = on_secondary_activate
        self._on_scroll = on_scroll
        self._on_menu_opening = on_menu_opening
        self._logger = logger
        self._bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._state = "unreachable"
        self._tooltip = ("LED Matrix", "")
        self._menu = MenuItem(0)
        self._revision = 1
        self._registered = False

        node = Gio.DBusNodeInfo.new_for_xml(_XML)
        self._registrations = [
            self._bus.register_object(
                ITEM_PATH,
                node.lookup_interface(ITEM_INTERFACE),
                self._on_item_call,
                self._on_item_property,
                None,
            ),
            self._bus.register_object(
                MENU_PATH,
                node.lookup_interface(MENU_INTERFACE),
                self._on_menu_call,
                self._on_menu_property,
                None,
            ),
        ]
        self._owner_id = Gio.bus_own_name_on_connection(
            self._bus, self._name, Gio.BusNameOwnerFlags.NONE, None, None
        )
        self._watch_id = Gio.bus_watch_name_on_connection(
            self._bus,
            WATCHER_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared,
            self._on_watcher_vanished,
        )

    @property
    def registered(self) -> bool:
        return self._registered

    def update(self, *, state: str, title: str, description: str, menu: MenuItem) -> None:
        if state != self._state:
            self._state = state
            self._emit(ITEM_PATH, ITEM_INTERFACE, "NewIcon")
        if (title, description) != self._tooltip:
            self._tooltip = (title, description)
            self._emit(ITEM_PATH, ITEM_INTERFACE, "NewToolTip")
        if layout(menu) != layout(self._menu):
            self._revision += 1
            self._menu = menu
            self._emit(
                MENU_PATH,
                MENU_INTERFACE,
                "LayoutUpdated",
                GLib.Variant("(ui)", (self._revision, 0)),
            )
        else:
            self._menu = menu  # same layout, fresh actions

    def close(self) -> None:
        Gio.bus_unwatch_name(self._watch_id)
        Gio.bus_unown_name(self._owner_id)
        for registration in self._registrations:
            self._bus.unregister_object(registration)

    # --- the item -----------------------------------------------------------------------

    def _on_item_call(self, _bus, _sender, _path, _interface, method, parameters, invocation):
        if method == "Activate":
            GLib.idle_add(self._run, self._on_activate)
        elif method == "SecondaryActivate":
            GLib.idle_add(self._run, self._on_secondary_activate)
        elif method == "Scroll":
            delta, orientation = parameters.unpack()
            if orientation.lower() == "vertical" and delta:
                GLib.idle_add(self._run, lambda: self._on_scroll(delta))
        invocation.return_value(None)

    def _on_item_property(self, _bus, _sender, _path, _interface, name):
        title, description = self._tooltip
        pixmaps = [(size, size, icon_argb32(self._state, size)) for size in ICON_SIZES]
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "framework-led-dashboard"),
            "Title": GLib.Variant("s", "LED Matrix"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": GLib.Variant("a(iiay)", pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], title, description)),
            "ItemIsMenu": GLib.Variant("b", False),  # left click activates, right click: menu
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    # --- the menu -----------------------------------------------------------------------

    def _on_menu_call(self, _bus, _sender, _path, _interface, method, parameters, invocation):
        if method == "GetLayout":
            parent_id, depth, names = parameters.unpack()
            parent = self._menu.find(parent_id) or self._menu
            node = _layout_variant(layout(parent, depth, names))
            invocation.return_value(
                GLib.Variant.new_tuple(GLib.Variant("u", self._revision), node)
            )
        elif method == "GetGroupProperties":
            ids, names = parameters.unpack()
            if ids:
                found = [self._menu.find(item_id) for item_id in ids]
            else:
                found = list(self._menu.walk())
            groups = []
            for item in found:
                if item is None:
                    continue
                _id, props, _children = layout(item, 0, names)
                groups.append((item.id, _property_variants(props)))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (groups,)))
        elif method == "GetProperty":
            item_id, name = parameters.unpack()
            item = self._menu.find(item_id)
            props = item.properties() if item else {}
            if name not in props:
                invocation.return_dbus_error(
                    "com.canonical.dbusmenu.Error", f"no property {name} on item {item_id}"
                )
                return
            kind, value = props[name]
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant(kind, value),)))
        elif method == "Event":
            item_id, event, _data, _timestamp = parameters.unpack()
            self._handle_event(item_id, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            (events,) = parameters.unpack()
            missing = [
                item_id
                for item_id, event, _data, _time in events
                if not self._handle_event(item_id, event)
            ]
            invocation.return_value(GLib.Variant("(ai)", (missing,)))
        elif method == "AboutToShow":
            (item_id,) = parameters.unpack()
            if item_id == 0:
                self._run(self._on_menu_opening)  # refresh, so the menu shows the current state
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _on_menu_property(self, _bus, _sender, _path, _interface, name):
        values = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }
        return values.get(name)

    def _handle_event(self, item_id: int, event: str) -> bool:
        item = self._menu.find(item_id)
        if item is None:
            return False
        if event == "clicked" and item.enabled and item.action is not None:
            GLib.idle_add(self._run, item.action)
        return True

    # --- registration -------------------------------------------------------------------

    def _on_watcher_appeared(self, bus, _name, _owner) -> None:
        bus.call(
            WATCHER_NAME,
            WATCHER_PATH,
            WATCHER_NAME,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._name,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_registered,
        )

    def _on_registered(self, bus, result) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            self._logger.warning("tray host refused the icon: %s", error.message)
            return
        self._registered = True

    def _on_watcher_vanished(self, _bus, _name) -> None:
        self._registered = False  # the panel went away; it registers again when it is back

    def _emit(self, path: str, interface: str, signal: str, parameters=None) -> None:
        try:
            self._bus.emit_signal(None, path, interface, signal, parameters)
        except GLib.Error as error:
            self._logger.debug("could not emit %s: %s", signal, error.message)

    def _run(self, callback: Callable[[], None]) -> bool:
        try:
            callback()
        except Exception:  # a broken callback must not take the tray down
            self._logger.exception("tray action failed")
        return False  # one-shot when used with idle_add
