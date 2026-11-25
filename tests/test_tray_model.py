from __future__ import annotations

import pytest

from framework_led_dashboard.control import DisplaySettings
from framework_led_dashboard.dbusmenu import (
    BRIGHTNESS_ID,
    POWER_ID,
    QUIT_ID,
    SETTINGS_ID,
    SWAP_ID,
    UNREACHABLE_ID,
    build_menu,
    layout,
)
from framework_led_dashboard.led_icon import icon_argb32, icon_rgba
from framework_led_dashboard.tray_model import choose_backend, tray_summary


class RecordingActions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def set_enabled(self, enabled: bool) -> None:
        self.calls.append(("enabled", enabled))

    def set_brightness(self, percent: int) -> None:
        self.calls.append(("brightness", percent))

    def set_swap(self, swapped: bool) -> None:
        self.calls.append(("swap", swapped))

    def open_settings(self) -> None:
        self.calls.append(("settings", None))

    def quit(self) -> None:
        self.calls.append(("quit", None))


def _pixel(data: bytes, size: int, x: int, y: int) -> tuple[int, ...]:
    index = (y * size + x) * 4
    return tuple(data[index : index + 4])


def test_icon_is_lit_amber_when_on_grey_when_off_and_transparent_around() -> None:
    on, off = icon_rgba("on", 64), icon_rgba("off", 64)

    assert len(on) == 64 * 64 * 4
    assert _pixel(on, 64, 0, 0)[3] == 0  # outside the module outline
    red, green, blue, alpha = _pixel(on, 64, 21, 52)  # a lit dot of the first bar
    assert alpha == 255 and red > 200 and blue < 100
    red, green, blue, alpha = _pixel(off, 64, 21, 52)
    assert alpha == 255 and red == green == blue


def test_unreachable_icon_carries_a_red_dot() -> None:
    red, green, _blue, alpha = _pixel(icon_rgba("unreachable", 64), 64, 50, 50)

    assert alpha == 255 and red > 200 and green < 100


def test_icon_argb32_is_rgba_with_alpha_moved_to_the_front() -> None:
    rgba, argb = icon_rgba("on", 24), icon_argb32("on", 24)

    assert argb[0::4] == rgba[3::4] and argb[1::4] == rgba[0::4] and argb[3::4] == rgba[2::4]


@pytest.mark.parametrize(("state", "size"), [("blinking", 32), ("on", 4)])
def test_icon_rejects_unknown_states_and_tiny_sizes(state: str, size: int) -> None:
    with pytest.raises(ValueError):
        icon_rgba(state, size)


def test_menu_shows_the_current_settings_with_check_marks_and_radio_lists() -> None:
    settings = DisplaySettings(40, swap_panels=True, cpu_alert_celsius=95)
    menu = build_menu(settings, RecordingActions())

    assert menu.find(UNREACHABLE_ID) is None
    assert menu.find(POWER_ID).properties()["toggle-state"] == ("i", 1)
    assert menu.find(SWAP_ID).properties()["toggle-state"] == ("i", 1)
    brightness = menu.find(BRIGHTNESS_ID)
    assert brightness.label == "Brightness: 40 %"
    assert [step.label for step in brightness.children if step.checked] == ["40 %"]


def test_menu_keeps_only_the_quick_controls() -> None:
    menu = build_menu(DisplaySettings(40), RecordingActions())

    assert [item.label for item in menu.children if not item.separator] == [
        "LED Matrix",
        "Brightness: 40 %",
        "Swap panels",
        "Settings…",
        "Quit tray",
    ]


def test_menu_items_call_the_matching_actions() -> None:
    actions = RecordingActions()
    menu = build_menu(DisplaySettings(40, enabled=True), actions)

    menu.find(POWER_ID).action()
    next(step for step in menu.find(BRIGHTNESS_ID).children if step.label == "80 %").action()
    menu.find(SWAP_ID).action()
    menu.find(SETTINGS_ID).action()
    menu.find(QUIT_ID).action()

    assert actions.calls == [
        ("enabled", False),
        ("brightness", 80),
        ("swap", True),
        ("settings", None),
        ("quit", None),
    ]


def test_menu_without_a_dashboard_says_so_and_disables_what_needs_it() -> None:
    menu = build_menu(None, RecordingActions())

    first = menu.children[0]
    assert (first.id, first.enabled) == (UNREACHABLE_ID, False)
    assert first.label == "Dashboard is not running"
    assert menu.find(POWER_ID).enabled is False
    assert menu.find(SWAP_ID).enabled is False
    assert next(item for item in menu.children if item.label == "Settings…").enabled is True
    assert next(item for item in menu.children if item.label == "Quit tray").enabled is True


def test_menu_ids_are_unique_and_do_not_move_when_settings_change() -> None:
    first = build_menu(DisplaySettings(40), RecordingActions())
    second = build_menu(
        DisplaySettings(77, enabled=False, swap_panels=True), RecordingActions()
    )

    ids = [item.id for item in first.walk()]
    assert len(ids) == len(set(ids))
    assert ids == [item.id for item in second.walk()]


def test_layout_follows_the_requested_depth_and_property_names() -> None:
    menu = build_menu(DisplaySettings(40), RecordingActions())

    root_id, _props, children = layout(menu, depth=1)
    assert root_id == 0
    assert all(grandchildren == [] for _id, _props, grandchildren in children)

    _id, props, _children = layout(menu.find(POWER_ID), names=("label",))
    assert props == {"label": ("s", "LED Matrix")}
    _id, props, _children = layout(menu.children[3])  # a separator
    assert props == {"type": ("s", "separator")}


XFCE_X11 = {"XDG_CURRENT_DESKTOP": "XFCE", "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
KDE_WAYLAND = {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland"}
GNOME_WAYLAND = {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "XDG_SESSION_TYPE": "wayland"}
GNOME_X11 = {"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}


@pytest.mark.parametrize(
    ("env", "watcher", "backend"),
    [
        (XFCE_X11, True, "xembed"),  # keeps the GTK popup with a real slider
        (XFCE_X11, False, "xembed"),
        (KDE_WAYLAND, True, "sni"),
        ({"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "x11"}, True, "sni"),
        (GNOME_WAYLAND, True, "sni"),  # with the AppIndicator extension
        (GNOME_WAYLAND, False, "none"),  # without it there is no tray at all
        (GNOME_X11, False, "none"),  # GNOME does not show XEmbed icons either
        ({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, False, "xembed"),  # a bare window manager
        ({"XDG_SESSION_TYPE": "wayland"}, False, "none"),
        ({**KDE_WAYLAND, "LED_DASHBOARD_TRAY": "xembed"}, True, "xembed"),
        ({**XFCE_X11, "LED_DASHBOARD_TRAY": " SNI "}, True, "sni"),
        ({**XFCE_X11, "LED_DASHBOARD_TRAY": "sparkles"}, True, "xembed"),  # unknown: ignored
    ],
)
def test_tray_backend_suits_the_desktop(env: dict[str, str], watcher: bool, backend: str) -> None:
    assert choose_backend(env, watcher_present=watcher) == backend


def test_tray_summary_names_state_brightness_sound_and_swap() -> None:
    assert tray_summary(None) == ("unreachable", "LED Matrix", "The dashboard is not running")
    state, title, description = tray_summary(
        DisplaySettings(55, enabled=False, swap_panels=True, audio_source="system")
    )
    assert (state, title) == ("off", "LED Matrix: off, brightness 55 %")
    assert description == "Equalizer: system sound, panels swapped"
    assert tray_summary(DisplaySettings(40))[2] == "Equalizer: microphone"
