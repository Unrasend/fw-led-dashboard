from __future__ import annotations

from framework_led_dashboard.alerts import AlertInputs, AlertPolicy
from framework_led_dashboard.config import AlertsConfig


def test_cpu_temperature_enters_at_120_and_recovers_only_below_115() -> None:
    policy = AlertPolicy(AlertsConfig())

    states = [
        policy.update(AlertInputs(cpu_temperature_c=value), now_seconds=index)
        for index, value in enumerate((105, 119, 120, 119, 115, 114))
    ]

    assert [state.cpu_temperature for state in states] == [
        False,
        False,
        True,
        True,
        True,
        False,
    ]


def test_cpu_temperature_follows_a_threshold_changed_at_runtime_with_the_configured_gap() -> None:
    threshold = [120.0]
    policy = AlertPolicy(
        AlertsConfig(cpu_temperature_critical_c=120, cpu_temperature_recovery_c=117),
        cpu_temperature_critical=lambda: threshold[0],
    )

    def alert(value: float, at: int) -> bool:
        return policy.update(AlertInputs(cpu_temperature_c=value), now_seconds=at).cpu_temperature

    assert alert(95, 0) is False
    threshold[0] = 90  # set from the tray
    assert alert(95, 1) is True
    assert alert(88, 2) is True  # still above 90 - 3
    assert alert(86.9, 3) is False
    threshold[0] = 100
    assert alert(95, 4) is False


def test_battery_enters_at_ten_percent_and_recovers_at_twelve_percent() -> None:
    policy = AlertPolicy(AlertsConfig())

    states = [
        policy.update(AlertInputs(battery_percent=value), now_seconds=index)
        for index, value in enumerate((11, 10, 11, 12))
    ]

    assert [state.battery for state in states] == [False, True, True, False]


def test_ram_requires_ten_continuous_seconds_below_available_threshold() -> None:
    policy = AlertPolicy(AlertsConfig())

    before_debounce = policy.update(
        AlertInputs(ram_available_percent=5), now_seconds=100
    )
    almost_due = policy.update(
        AlertInputs(ram_available_percent=5), now_seconds=109.999
    )
    due = policy.update(AlertInputs(ram_available_percent=5), now_seconds=110)
    hysteresis = policy.update(AlertInputs(ram_available_percent=6), now_seconds=111)
    recovered = policy.update(AlertInputs(ram_available_percent=8), now_seconds=112)

    assert before_debounce.ram is False
    assert almost_due.ram is False
    assert due.ram is True
    assert hysteresis.ram is True
    assert recovered.ram is False


def test_ram_debounce_restarts_when_low_memory_condition_is_interrupted() -> None:
    policy = AlertPolicy(AlertsConfig())

    policy.update(AlertInputs(ram_available_percent=5), now_seconds=0)
    policy.update(AlertInputs(ram_available_percent=6), now_seconds=9)
    restarted = policy.update(AlertInputs(ram_available_percent=5), now_seconds=10)
    almost_due = policy.update(AlertInputs(ram_available_percent=5), now_seconds=19.999)
    due = policy.update(AlertInputs(ram_available_percent=5), now_seconds=20)

    assert restarted.ram is False
    assert almost_due.ram is False
    assert due.ram is True


def test_missing_and_non_finite_values_never_remain_critical() -> None:
    policy = AlertPolicy(AlertsConfig())
    policy.update(
        AlertInputs(
            battery_percent=5,
            cpu_temperature_c=125,
            ram_available_percent=2,
        ),
        now_seconds=0,
    )
    policy.update(AlertInputs(ram_available_percent=2), now_seconds=10)

    missing = policy.update(AlertInputs(), now_seconds=11)
    non_finite = policy.update(
        AlertInputs(
            battery_percent=float("nan"),
            cpu_temperature_c=float("inf"),
            ram_available_percent=float("-inf"),
        ),
        now_seconds=12,
    )

    assert missing.any_critical is False
    assert non_finite.any_critical is False


def test_optional_thresholds_trigger_only_when_configured() -> None:
    disabled = AlertPolicy(AlertsConfig()).update(
        AlertInputs(
            cpu_usage_percent=100,
            outside_temperature_c=100,
        ),
        now_seconds=0,
    )
    policy = AlertPolicy(
        AlertsConfig(
            cpu_usage_critical_percent=95,
            outside_temperature_min_c=-20,
            outside_temperature_max_c=38,
        )
    )

    upper = policy.update(
        AlertInputs(
            cpu_usage_percent=95,
            outside_temperature_c=38,
        ),
        now_seconds=0,
    )
    lower = policy.update(
        AlertInputs(
            cpu_usage_percent=94.9,
            outside_temperature_c=-20,
        ),
        now_seconds=1,
    )
    normal = policy.update(
        AlertInputs(
            cpu_usage_percent=50,
            outside_temperature_c=18,
        ),
        now_seconds=2,
    )

    assert disabled.cpu_usage is False
    assert disabled.outside_temperature is False
    assert upper.cpu_usage is True
    assert upper.outside_temperature is True
    assert lower.cpu_usage is False
    assert lower.outside_temperature is True
    assert normal.any_critical is False
