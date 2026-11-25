"""Which bars blink: each bar's rule (layout.BlinkRule) against its source's value.

A rule that holds starts the blink; it stops only once the value is HYSTERESIS points back,
so a value sitting right at the threshold does not switch the blink on and off every second.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from .bar_sources import BAR_SOURCES
from .layout import BlinkRule
from .system_metrics import SystemMetrics

HYSTERESIS = 2.0  # percentage points
BarRule = tuple[str, BlinkRule]  # (source key, rule)


class BarBlinker:
    def __init__(self, *, hysteresis: float = HYSTERESIS) -> None:
        self._hysteresis = hysteresis
        self._blinking: frozenset[BarRule] = frozenset()

    @property
    def blinking(self) -> frozenset[BarRule]:
        return self._blinking

    def update(self, rules: Iterable[BarRule], metrics: SystemMetrics) -> frozenset[BarRule]:
        blinking = set()
        for key, rule in set(rules):
            if rule.when == "never":
                continue
            value = BAR_SOURCES[key].read(metrics)
            if value is None or not math.isfinite(value):
                continue
            margin = self._hysteresis if (key, rule) in self._blinking else 0.0
            if rule.when == "above":
                holds = value >= rule.percent - margin
            else:
                holds = value <= rule.percent + margin
            if holds:
                blinking.add((key, rule))
        self._blinking = frozenset(blinking)
        return self._blinking


def describe_rule(key: str, rule: BlinkRule) -> str:
    sign = "≥" if rule.when == "above" else "≤"
    return f"{BAR_SOURCES[key].label} {sign} {rule.percent} %"
