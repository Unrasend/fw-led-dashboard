from __future__ import annotations

import logging

import pytest

from framework_led_dashboard.scheduler import blink_on, run_loop, weather_frame_index


class ScriptedStop:
    """Record every requested wait and report a stop request after `limit` waits."""

    def __init__(self, limit: int) -> None:
        self.waits: list[float] = []
        self._limit = limit

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return len(self.waits) > self._limit


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (0.0, True),
        (0.49, True),
        (0.5, False),
        (0.99, False),
        (1.0, True),
        (12.3, True),
        (12.7, False),
    ],
)
def test_blink_is_one_hertz_and_on_for_the_first_half_of_each_second(
    elapsed: float,
    expected: bool,
) -> None:
    assert blink_on(elapsed) is expected


def test_weather_icon_steps_twice_per_second() -> None:
    assert [weather_frame_index(t) for t in (0.0, 0.49, 0.5, 1.0, 9.99)] == [0, 0, 1, 2, 19]


def test_run_loop_waits_for_each_delay_the_step_returns() -> None:
    delays = iter([0.1, 0.25, 3.0])
    stop = ScriptedStop(limit=3)

    run_loop(lambda: next(delays), stop, name="test", initial_delay_seconds=2.0)

    assert stop.waits == [2.0, 0.1, 0.25, 3.0]


def test_run_loop_survives_a_failing_step_and_backs_off(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[int] = []

    def step() -> float:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("sensor exploded")
        return 0.5

    stop = ScriptedStop(limit=2)
    with caplog.at_level(logging.ERROR):
        run_loop(step, stop, name="sensors", error_delay_seconds=1.5)

    assert stop.waits == [0.0, 1.5, 0.5]
    assert "sensors" in caplog.text
    assert "sensor exploded" in caplog.text


def test_run_loop_never_steps_after_stop_is_requested() -> None:
    calls: list[int] = []

    run_loop(lambda: calls.append(1) or 1.0, ScriptedStop(limit=0), name="idle")

    assert calls == []
