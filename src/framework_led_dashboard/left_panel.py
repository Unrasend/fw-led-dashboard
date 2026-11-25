"""The left panel as it looked before layouts: kept for the demo page, the README GIF and the
golden frames, which check that the layout renderer still draws it pixel for pixel."""

from __future__ import annotations

from collections.abc import Sequence

from .alerts import AlertState
from .bar_sources import DEFAULT_BARS
from .frame import Frame
from .layout import classic_left
from .panel_render import PanelData, render_panel
from .system_metrics import SystemMetrics


def render_left_panel(
    metrics: SystemMetrics,
    *,
    bars: Sequence[str] = DEFAULT_BARS,
    spectrum: Sequence[float] | None = None,
    alerts: AlertState | None = None,
    blink_on: bool = True,
) -> Frame:
    return render_panel(
        classic_left(bars),
        PanelData(metrics=metrics, alerts=alerts or AlertState()),
        spectrum=spectrum,
        mirrored=True,
        blink_on=blink_on,
    )
