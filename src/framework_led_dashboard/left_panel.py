from __future__ import annotations

from collections.abc import Sequence

from .alerts import AlertState
from .bar_sources import BAR_SOURCES, DEFAULT_BARS, validate_bars
from .equalizer import draw_equalizer
from .frame import FRAME_WIDTH, Frame
from .system_metrics import SystemMetrics

BAR_INTENSITY = 220
UNKNOWN_INTENSITY = 24
BAR_TOP = 0
BAR_BOTTOM = 20
BAR_HEIGHT = BAR_BOTTOM - BAR_TOP + 1
BASE_ROW = 21
BATTERY_COLUMNS = (0, 1)
CPU_COLUMNS = (3, 4, 5)
RAM_COLUMNS = (7, 8)
BAR_SLOT_COLUMNS = (BATTERY_COLUMNS, CPU_COLUMNS, RAM_COLUMNS)


def render_left_panel(
    metrics: SystemMetrics,
    *,
    bars: Sequence[str] = DEFAULT_BARS,
    spectrum: Sequence[float] | None = None,
    alerts: AlertState | None = None,
    blink_on: bool = True,
) -> Frame:
    alerts = alerts or AlertState()
    sources = [BAR_SOURCES[key] for key in validate_bars(bars)]
    frame = Frame()
    frame.fill_rect(x=0, y=BASE_ROW, width=FRAME_WIDTH, height=1, value=BAR_INTENSITY)
    for columns, source in zip(BAR_SLOT_COLUMNS, sources, strict=True):
        # A critical source blinks in whichever bar shows it.
        if blink_on or not source.critical(alerts):
            _draw_bar(frame, columns, source.read(metrics))
    draw_equalizer(frame, spectrum, mirrored=True)
    return frame


def _draw_bar(frame: Frame, columns: tuple[int, ...], percent: float | None) -> None:
    if percent is None:
        frame.fill_rect(x=columns[0], y=BAR_BOTTOM, width=len(columns), height=1, value=UNKNOWN_INTENSITY)
        return

    clamped_percent = max(0.0, min(100.0, percent))
    filled_rows = int(clamped_percent * BAR_HEIGHT / 100 + 0.5)
    if filled_rows == 0:
        return
    frame.fill_rect(
        x=columns[0],
        y=BAR_BOTTOM - filled_rows + 1,
        width=len(columns),
        height=filled_rows,
        value=BAR_INTENSITY,
    )
