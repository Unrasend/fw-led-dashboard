"""Tray decisions that need no GTK: which kind of tray icon to use, and what it says."""

from __future__ import annotations

from collections.abc import Mapping

from .control import DisplaySettings

# Desktops whose panel shows classic X11 (XEmbed) tray icons natively. There the tray keeps its
# GTK popup with a real switch and slider; everywhere else it uses StatusNotifierItem.
XEMBED_DESKTOPS = frozenset({"XFCE", "MATE", "X-CINNAMON", "CINNAMON", "LXDE"})
BACKENDS = ("xembed", "sni", "none")
SWAP_HINT = (
    "Each panel shows the other one's widgets.\n"
    "Also puts things right if the panels were detected the wrong way round."
)


def choose_backend(env: Mapping[str, str], *, watcher_present: bool) -> str:
    """"xembed", "sni" or "none"; LED_DASHBOARD_TRAY=xembed|sni|none overrides the guess."""
    forced = env.get("LED_DASHBOARD_TRAY", "").strip().lower()
    if forced in BACKENDS:
        return forced
    desktops = {name.upper() for name in env.get("XDG_CURRENT_DESKTOP", "").split(":") if name}
    session = env.get("XDG_SESSION_TYPE", "").lower()
    no_session_type = not session and not env.get("WAYLAND_DISPLAY")
    on_x11 = session == "x11" or (no_session_type and bool(env.get("DISPLAY")))
    if on_x11 and desktops & XEMBED_DESKTOPS:
        return "xembed"
    if watcher_present:
        return "sni"
    # GNOME dropped XEmbed icons in 3.26: without the AppIndicator extension there is no tray.
    if on_x11 and "GNOME" not in desktops:
        return "xembed"
    return "none"


def tray_summary(settings: DisplaySettings | None) -> tuple[str, str, str]:
    """(icon state, title, description) for the icon and its tooltip."""
    if settings is None:
        return "unreachable", "LED Matrix", "The dashboard is not running"
    state = "on" if settings.enabled else "off"
    sound = "microphone" if settings.audio_source == "microphone" else "system sound"
    swapped = ", panels swapped" if settings.swap_panels else ""
    return (
        state,
        f"LED Matrix: {state}, brightness {settings.brightness_percent} %",
        f"Equalizer: {sound}{swapped}",
    )
