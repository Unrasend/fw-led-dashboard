"""End-to-end check of the StatusNotifierItem tray on a private D-Bus session:

    dbus-run-session -- python3 scripts/sni_smoke.py

It plays the desktop's tray host: a stand-in StatusNotifierWatcher takes the icon's
registration, then a second bus connection reads the icon and the menu and clicks items,
like KDE Plasma or GNOME's AppIndicator extension would. The clicks must reach a real
control server. Prints the observations as JSON; tests/test_sni_bus.py checks them.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk

from framework_led_dashboard.control import (
    ControlClient,
    ControlServer,
    DisplaySettings,
    SettingsStore,
)
from framework_led_dashboard.dbusmenu import POWER_ID, SWAP_ID
from framework_led_dashboard.scheduler import run_loop
from framework_led_dashboard.tray import SniTray

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>
"""


def wait_for(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def main() -> None:
    socket_path = Path(tempfile.mkdtemp()) / "control.sock"
    store = SettingsStore(DisplaySettings(40))
    server = ControlServer(store, path=socket_path)
    server.open()
    stop = threading.Event()
    threading.Thread(
        target=run_loop, args=(server.step, stop), kwargs={"name": "control"}, daemon=True
    ).start()

    Gtk.init_check([])
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    registered: list[str] = []

    def watcher_call(_bus, _sender, _path, _interface, _method, parameters, invocation):
        registered.append(parameters.unpack()[0])
        invocation.return_value(None)

    node = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML)
    bus.register_object("/StatusNotifierWatcher", node.interfaces[0], watcher_call, None, None)
    Gio.bus_own_name_on_connection(
        bus, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE, None, None
    )

    events: list[str] = []
    SniTray(
        ControlClient(socket_path),
        open_settings=lambda: events.append("settings"),
        quit=lambda: events.append("quit"),
        on_change=lambda _settings: None,
    )
    results: dict[str, object] = {}
    loop = GLib.MainLoop()

    def play_the_host() -> None:
        try:
            results.update(host_session(registered, store, events))
        except Exception as error:  # noqa: BLE001 - any failure is reported, never a hang
            results["error"] = repr(error)
        GLib.idle_add(loop.quit)

    threading.Thread(target=play_the_host, daemon=True).start()
    GLib.timeout_add_seconds(20, loop.quit)
    loop.run()
    stop.set()
    server.close()
    print(json.dumps(results))


def host_session(registered: list[str], store: SettingsStore, events: list[str]) -> dict:
    panel = Gio.DBusConnection.new_for_address_sync(
        os.environ["DBUS_SESSION_BUS_ADDRESS"],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )
    if not wait_for(lambda: registered):
        return {"error": "the tray never registered with the watcher"}
    service = registered[0]

    def call(path, interface, method, args, reply=None):
        reply_type = GLib.VariantType(reply) if reply else None
        result = panel.call_sync(
            service, path, interface, method, args, reply_type, Gio.DBusCallFlags.NONE, 3000, None
        )
        return result.unpack() if result is not None else None

    def item_property(name):
        args = GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", name))
        return call("/StatusNotifierItem", "org.freedesktop.DBus.Properties", "Get", args, "(v)")[0]

    def menu_layout():
        args = GLib.Variant("(iias)", (0, -1, []))
        return call("/MenuBar", "com.canonical.dbusmenu", "GetLayout", args, "(u(ia{sv}av))")

    def click(item_id):
        args = GLib.Variant("(isvu)", (item_id, "clicked", GLib.Variant("s", ""), 0))
        call("/MenuBar", "com.canonical.dbusmenu", "Event", args)

    results: dict[str, object] = {"service": service}
    results["tooltip"] = list(item_property("ToolTip"))[2:]
    results["pixmaps"] = [(w, h, len(data)) for w, h, data in item_property("IconPixmap")]
    results["menu_path"] = item_property("Menu")
    revision, (root_id, _props, children) = menu_layout()
    results["labels"] = [props.get("label", "---") for _id, props, _children in children]

    click(POWER_ID)
    results["power_after_click"] = wait_for(lambda: store.current().enabled is False)
    click(SWAP_ID)
    results["swap_after_click"] = wait_for(lambda: store.current().swap_panels is True)
    call("/StatusNotifierItem", "org.kde.StatusNotifierItem", "Scroll",
         GLib.Variant("(is)", (120, "vertical")))
    results["brightness_after_scroll"] = wait_for(
        lambda: store.current().brightness_percent == 45
    )
    call("/StatusNotifierItem", "org.kde.StatusNotifierItem", "Activate",
         GLib.Variant("(ii)", (0, 0)))
    results["activate_opened_settings"] = wait_for(lambda: "settings" in events)

    def updated_toggles():
        new_revision, (_root, _props, items) = menu_layout()
        toggles = {item_id: props.get("toggle-state") for item_id, props, _c in items}
        return new_revision > revision and toggles.get(POWER_ID) == 0 and toggles.get(SWAP_ID) == 1

    results["menu_updated"] = wait_for(updated_toggles)
    results["root_id"] = root_id
    return results


if __name__ == "__main__":
    main()
