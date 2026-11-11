"""Timing shared by the dashboard loops: blink phase, weather icon frames and the loop runner."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

LOGGER = logging.getLogger("framework_led_dashboard")

BLINK_PERIOD_SECONDS = 1.0
WEATHER_ICON_FPS = 2

Step = Callable[[], float]


class StopSignal(Protocol):
    def wait(self, timeout: float | None = None) -> bool: ...


def blink_on(elapsed_seconds: float) -> bool:
    """Critical widgets blink at 1 Hz: visible for the first half of every second."""
    return elapsed_seconds % BLINK_PERIOD_SECONDS < BLINK_PERIOD_SECONDS / 2


def weather_frame_index(elapsed_seconds: float) -> int:
    return int(elapsed_seconds * WEATHER_ICON_FPS)


def run_loop(
    step: Step,
    stop: StopSignal,
    *,
    name: str,
    initial_delay_seconds: float = 0.0,
    error_delay_seconds: float = 1.0,
    logger: logging.Logger = LOGGER,
) -> None:
    """Call `step` until `stop` is set, waiting the delay each call returns.

    A failing step is logged and retried after `error_delay_seconds`, so one broken provider
    neither kills its thread nor spins the CPU.
    """
    delay = initial_delay_seconds
    while not stop.wait(delay):
        try:
            delay = step()
        except Exception:
            logger.exception("%s loop step failed", name)
            delay = error_delay_seconds
