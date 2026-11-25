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


def test_missing_and_non_finite_temperatures_never_remain_critical() -> None:
    policy = AlertPolicy(AlertsConfig(outside_temperature_max_c=30))
    policy.update(AlertInputs(cpu_temperature_c=125, outside_temperature_c=35), now_seconds=0)

    missing = policy.update(AlertInputs(), now_seconds=11)
    non_finite = policy.update(
        AlertInputs(cpu_temperature_c=float("inf"), outside_temperature_c=float("nan")),
        now_seconds=12,
    )

    assert missing.any_critical is False
    assert non_finite.any_critical is False


def test_outside_temperature_limits_trigger_only_when_configured() -> None:
    disabled = AlertPolicy(AlertsConfig()).update(
        AlertInputs(outside_temperature_c=100), now_seconds=0
    )
    policy = AlertPolicy(AlertsConfig(outside_temperature_min_c=-20, outside_temperature_max_c=38))

    assert disabled.outside_temperature is False
    assert policy.update(AlertInputs(outside_temperature_c=38), now_seconds=0).outside_temperature
    assert not policy.update(AlertInputs(outside_temperature_c=37.9), now_seconds=1).outside_temperature
    assert policy.update(AlertInputs(outside_temperature_c=-20), now_seconds=2).outside_temperature


def test_old_bar_alert_settings_still_load_but_no_longer_drive_anything() -> None:
    # Bars blink by their own rules now (bar_alerts); a config from 1.4 still parses.
    policy = AlertPolicy(AlertsConfig(battery_critical_percent=50, cpu_usage_critical_percent=10))

    assert policy.update(AlertInputs(cpu_temperature_c=60), now_seconds=0).any_critical is False
