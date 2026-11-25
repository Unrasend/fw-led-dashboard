"""The right panel as it looked before layouts (see left_panel.py)."""

from __future__ import annotations

from collections.abc import Sequence

from .alerts import AlertState
from .frame import Frame
from .layout import classic_right
from .panel_render import PanelData, render_panel
from .weather import WeatherReading


def render_right_panel(
    *,
    cpu_temperature_c: float | None,
    weather: WeatherReading | None = None,
    weather_frame: int = 0,
    spectrum: Sequence[float] | None = None,
    alerts: AlertState | None = None,
    blink_on: bool = True,
) -> Frame:
    return render_panel(
        classic_right(),
        PanelData(
            cpu_temperature_c=cpu_temperature_c, weather=weather, alerts=alerts or AlertState()
        ),
        spectrum=spectrum,
        weather_frame=weather_frame,
        blink_on=blink_on,
    )
