"""The left panel as it looked before layouts: kept for the demo page, the README GIF and the
golden frames, which check that the layout renderer still draws it pixel for pixel."""

from __future__ import annotations

from collections.abc import Collection, Sequence

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
    blinking: Collection[str] = (),
    blink_on: bool = True,
) -> Frame:
    """`blinking`: the sources whose bars blink (their rule holds)."""
    widgets = classic_left(bars)
    rules = widgets[0]
    return render_panel(
        widgets,
        PanelData(
            metrics=metrics,
            blinking_bars=frozenset(
                (key, rule) for key, rule in zip(rules.bars, rules.blink, strict=True) if key in blinking
            ),
        ),
        spectrum=spectrum,
        mirrored=True,
        blink_on=blink_on,
    )
