from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .config import AlertsConfig


@dataclass(frozen=True, slots=True)
class AlertInputs:
    battery_percent: float | None = None
    cpu_usage_percent: float | None = None
    ram_available_percent: float | None = None
    cpu_temperature_c: float | None = None
    outside_temperature_c: float | None = None


@dataclass(frozen=True, slots=True)
class AlertState:
    battery: bool = False
    cpu_usage: bool = False
    ram: bool = False
    cpu_temperature: bool = False
    outside_temperature: bool = False

    @property
    def any_critical(self) -> bool:
        return any(
            (
                self.battery,
                self.cpu_usage,
                self.ram,
                self.cpu_temperature,
                self.outside_temperature,
            )
        )


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
        self._ram_low_since: float | None = None

    def update(self, inputs: AlertInputs, *, now_seconds: float) -> AlertState:
        if not _is_known(now_seconds):
            raise ValueError("alert timestamp must be finite")

        self._state = AlertState(
            battery=_low_with_hysteresis(
                inputs.battery_percent,
                active=self._state.battery,
                critical=self._config.battery_critical_percent,
                recovery=self._config.battery_recovery_percent,
            ),
            cpu_usage=_optional_high(
                inputs.cpu_usage_percent,
                self._config.cpu_usage_critical_percent,
            ),
            ram=self._update_ram(inputs.ram_available_percent, now_seconds),
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

    def _update_ram(self, available_percent: float | None, now_seconds: float) -> bool:
        if not _is_known(available_percent):
            self._ram_low_since = None
            return False

        if self._state.ram:
            if available_percent >= self._config.ram_available_recovery_percent:
                self._ram_low_since = None
                return False
            return True

        if available_percent > self._config.ram_available_critical_percent:
            self._ram_low_since = None
            return False

        if self._ram_low_since is None:
            self._ram_low_since = now_seconds
        return now_seconds - self._ram_low_since >= self._config.ram_critical_seconds


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


def _low_with_hysteresis(
    value: float | None,
    *,
    active: bool,
    critical: float,
    recovery: float,
) -> bool:
    if not _is_known(value):
        return False
    if active:
        return value < recovery
    return value <= critical


def _optional_high(value: float | None, critical: float | None) -> bool:
    return critical is not None and _is_known(value) and value >= critical


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
