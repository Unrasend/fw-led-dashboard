from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .config import AlertsConfig


@dataclass(frozen=True, slots=True)
class AlertInputs:
    cpu_temperature_c: float | None = None
    outside_temperature_c: float | None = None


@dataclass(frozen=True, slots=True)
class AlertState:
    """The temperatures that blink. Bars blink by their own rules (bar_alerts)."""

    cpu_temperature: bool = False
    outside_temperature: bool = False

    @property
    def any_critical(self) -> bool:
        return self.cpu_temperature or self.outside_temperature


class AlertPolicy:
    """Evaluate sensor values without reading clocks, devices or global state.

    `cpu_temperature_critical`, when given, is asked for the CPU threshold on every update
    (the tray can change it); recovery then stays the configured distance below it.
    """

    def __init__(
        self,
        config: AlertsConfig,
        *,
        cpu_temperature_critical: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._cpu_critical = cpu_temperature_critical or (
            lambda: config.cpu_temperature_critical_c
        )
        self._cpu_recovery_gap = (
            config.cpu_temperature_critical_c - config.cpu_temperature_recovery_c
        )
        self._state = AlertState()

    def update(self, inputs: AlertInputs, *, now_seconds: float) -> AlertState:
        if not _is_known(now_seconds):
            raise ValueError("alert timestamp must be finite")

        self._state = AlertState(
            cpu_temperature=self._cpu_temperature_alert(inputs.cpu_temperature_c),
            outside_temperature=_outside_range(
                inputs.outside_temperature_c,
                minimum=self._config.outside_temperature_min_c,
                maximum=self._config.outside_temperature_max_c,
            ),
        )
        return self._state

    def _cpu_temperature_alert(self, temperature_c: float | None) -> bool:
        critical = self._cpu_critical()
        return _high_with_hysteresis(
            temperature_c,
            active=self._state.cpu_temperature,
            critical=critical,
            recovery=critical - self._cpu_recovery_gap,
        )


def _high_with_hysteresis(
    value: float | None,
    *,
    active: bool,
    critical: float,
    recovery: float,
) -> bool:
    if not _is_known(value):
        return False
    if active:
        return value >= recovery
    return value >= critical


def _outside_range(
    value: float | None,
    *,
    minimum: float | None,
    maximum: float | None,
) -> bool:
    if not _is_known(value):
        return False
    return (minimum is not None and value <= minimum) or (
        maximum is not None and value >= maximum
    )


def _is_known(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )
