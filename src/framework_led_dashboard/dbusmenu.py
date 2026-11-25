"""The tray menu as a plain tree, for the com.canonical.dbusmenu protocol.

StatusNotifierItem hosts (KDE Plasma, GNOME's AppIndicator extension, Cinnamon, ...) draw the
menu themselves from this tree, so it can only hold standard items: labels, check marks,
radio lists and submenus. The settings window has the full controls.

Item ids are fixed by position, so a host that caches them stays right after an update.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .bar_sources import BAR_SLOT_NAMES, BAR_SOURCES
from .control import DisplaySettings, bars_side

BRIGHTNESS_STEPS = (5, 10, 20, 30, 40, 50, 60, 80, 100)

UNREACHABLE_ID = 1
POWER_ID = 2
BRIGHTNESS_ID = 3
SWAP_ID = 4
BARS_TITLE_ID = 6
BAR_IDS = (7, 8, 9)
CPU_ALERT_ID = 11
SETTINGS_ID = 12
QUIT_ID = 14
_BRIGHTNESS_STEP_BASE = 100
_BAR_SOURCE_BASE = 200


class MenuActions(Protocol):
    def set_enabled(self, enabled: bool) -> None: ...
    def set_brightness(self, percent: int) -> None: ...
    def set_swap(self, swapped: bool) -> None: ...
    def set_bar(self, slot: int, key: str) -> None: ...
    def open_cpu_alert(self) -> None: ...
    def open_settings(self) -> None: ...
    def quit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MenuItem:
    id: int
    label: str = ""
    separator: bool = False
    enabled: bool = True
    toggle: str = ""  # "", "checkmark" or "radio"
    checked: bool = False
    children: tuple[MenuItem, ...] = ()
    action: Callable[[], None] | None = field(default=None, compare=False)

    def properties(self) -> dict[str, tuple[str, object]]:
        """dbusmenu properties as {name: (GVariant type, value)}."""
        if self.separator:
            return {"type": ("s", "separator")}
        props: dict[str, tuple[str, object]] = {
            "label": ("s", self.label),
            "enabled": ("b", self.enabled),
            "visible": ("b", True),
        }
        if self.toggle:
            props["toggle-type"] = ("s", self.toggle)
            props["toggle-state"] = ("i", 1 if self.checked else 0)
        if self.children:
            props["children-display"] = ("s", "submenu")
        return props

    def walk(self) -> Iterator[MenuItem]:
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, item_id: int) -> MenuItem | None:
        return next((item for item in self.walk() if item.id == item_id), None)


Layout = tuple[int, dict[str, tuple[str, object]], list["Layout"]]


def layout(item: MenuItem, depth: int = -1, names: Sequence[str] = ()) -> Layout:
    """GetLayout's (id, properties, children); depth -1 means the whole subtree."""
    props = item.properties()
    if names:
        props = {name: value for name, value in props.items() if name in names}
    children = [] if depth == 0 else [layout(child, depth - 1, names) for child in item.children]
    return item.id, props, children


def build_menu(settings: DisplaySettings | None, actions: MenuActions) -> MenuItem:
    reachable = settings is not None
    settings = settings or DisplaySettings(40)
    items = [
        MenuItem(UNREACHABLE_ID, "Dashboard is not running", enabled=False)
        if not reachable
        else None,
        MenuItem(
            POWER_ID,
            "LED Matrix",
            enabled=reachable,
            toggle="checkmark",
            checked=settings.enabled,
            action=lambda: actions.set_enabled(not settings.enabled),
        ),
        _brightness_menu(settings, actions, reachable),
        MenuItem(
            SWAP_ID,
            "Swap panels",
            enabled=reachable,
            toggle="checkmark",
            checked=settings.swap_panels,
            action=lambda: actions.set_swap(not settings.swap_panels),
        ),
        MenuItem(5, separator=True),
        MenuItem(BARS_TITLE_ID, f"{bars_side(settings).capitalize()} panel bars", enabled=False),
        *(_bar_menu(slot, key, actions, reachable) for slot, key in enumerate(settings.bars)),
        MenuItem(10, separator=True),
        MenuItem(
            CPU_ALERT_ID,
            f"CPU alert: {settings.cpu_alert_celsius:g} °C…",
            enabled=reachable,
            action=actions.open_cpu_alert,
        ),
        MenuItem(SETTINGS_ID, "Settings…", action=actions.open_settings),
        MenuItem(13, separator=True),
        MenuItem(QUIT_ID, "Quit tray", action=actions.quit),
    ]
    return MenuItem(0, children=tuple(item for item in items if item is not None))


def _brightness_menu(
    settings: DisplaySettings, actions: MenuActions, reachable: bool
) -> MenuItem:
    steps = tuple(
        MenuItem(
            _BRIGHTNESS_STEP_BASE + index,
            f"{percent} %",
            toggle="radio",
            checked=settings.brightness_percent == percent,
            action=lambda percent=percent: actions.set_brightness(percent),
        )
        for index, percent in enumerate(BRIGHTNESS_STEPS)
    )
    return MenuItem(
        BRIGHTNESS_ID,
        f"Brightness: {settings.brightness_percent} %",
        enabled=reachable,
        children=steps,
    )


def _bar_menu(slot: int, current: str, actions: MenuActions, reachable: bool) -> MenuItem:
    sources = tuple(
        MenuItem(
            _BAR_SOURCE_BASE + slot * 20 + index,
            source.label,
            toggle="radio",
            checked=key == current,
            action=lambda key=key: actions.set_bar(slot, key),
        )
        for index, (key, source) in enumerate(BAR_SOURCES.items())
    )
    return MenuItem(
        BAR_IDS[slot],
        f"{BAR_SLOT_NAMES[slot]}: {BAR_SOURCES[current].label}",
        enabled=reachable,
        children=sources,
    )
