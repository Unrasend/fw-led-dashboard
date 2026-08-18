from __future__ import annotations

from .frame import Frame
from .life import LIFE_HEIGHT, LIFE_WIDTH, LifeGrid
from .system_metrics import SystemMetrics


LIFE_INTENSITY = 128
BAR_INTENSITY = 220
UNKNOWN_INTENSITY = 24
BAR_TOP = 13
BAR_BOTTOM = 33
BAR_HEIGHT = BAR_BOTTOM - BAR_TOP + 1
BATTERY_COLUMNS = (0, 1)
CPU_COLUMNS = (3, 4)
RAM_COLUMNS = (6, 7)


def render_left_panel(life_state: LifeGrid, metrics: SystemMetrics) -> Frame:
    frame = Frame()
    _draw_life(frame, life_state)
    _draw_bar(frame, BATTERY_COLUMNS, metrics.battery_percent)
    _draw_bar(frame, CPU_COLUMNS, metrics.cpu_percent)
    _draw_bar(frame, RAM_COLUMNS, metrics.ram_used_percent)
    return frame


def _draw_life(frame: Frame, life_state: LifeGrid) -> None:
    if len(life_state) != LIFE_HEIGHT or any(len(row) != LIFE_WIDTH for row in life_state):
        raise ValueError(f"Life grid must be {LIFE_WIDTH}x{LIFE_HEIGHT}")
    for y, row in enumerate(life_state):
        for x, alive in enumerate(row):
            if alive:
                frame.set_pixel(x, y, LIFE_INTENSITY)


def _draw_bar(frame: Frame, columns: tuple[int, int], percent: float | None) -> None:
    if percent is None:
        frame.fill_rect(
            x=columns[0],
            y=BAR_BOTTOM,
            width=len(columns),
            height=1,
            value=UNKNOWN_INTENSITY,
        )
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
