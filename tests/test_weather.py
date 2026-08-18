from __future__ import annotations

from framework_led_dashboard.config import LocationConfig
from framework_led_dashboard.location import (
    CachedLocation,
    Coordinates,
    LocationResolver,
    LocationSource,
    VpnDetector,
    VpnState,
)


def test_vpn_detector_reports_active_for_networkmanager_vpn() -> None:
    outputs = {
        "nmcli": "802-11-wireless:wlp1s0\nvpn:tun0\n",
        "route": "default via 192.0.2.1 dev wlp1s0\n",
        "link": "2: wlp1s0: <UP> state UP\n7: tun0: <UP> state UNKNOWN\n",
    }

    detector = VpnDetector(command_runner=lambda command: outputs[_probe_name(command)])

    assert detector.status() is VpnState.ACTIVE


def test_vpn_detector_covers_wireguard_default_route_and_interface_prefixes() -> None:
    cases = (
        ("wireguard:wg0\n", "default dev eth0\n", "2: eth0: <UP>\n"),
        ("802-3-ethernet:eth0\n", "default dev tailscale0\n", "2: eth0: <UP>\n"),
        ("802-3-ethernet:eth0\n", "default dev eth0\n", "7: mullvad0: <UP>\n"),
    )

    for nmcli, route, link in cases:
        outputs = {"nmcli": nmcli, "route": route, "link": link}
        detector = VpnDetector(
            command_runner=lambda command, current=outputs: current[_probe_name(command)]
        )

        assert detector.status() is VpnState.ACTIVE


def test_vpn_detector_is_unknown_when_any_local_probe_fails() -> None:
    def broken_runner(command: tuple[str, ...]) -> str:
        if command[0] == "nmcli":
            raise OSError("NetworkManager unavailable")
        return ""

    assert VpnDetector(command_runner=broken_runner).status() is VpnState.UNKNOWN


def test_location_override_bypasses_vpn_and_network() -> None:
    resolver = LocationResolver(
        LocationConfig(latitude=50.0755, longitude=14.4378),
        vpn_status=lambda: (_ for _ in ()).throw(AssertionError("VPN probe called")),
        ip_lookup=lambda: (_ for _ in ()).throw(AssertionError("IP lookup called")),
    )

    assert resolver.resolve() == Coordinates(50.0755, 14.4378, LocationSource.EXPLICIT)


def test_active_or_unknown_vpn_skips_ip_and_uses_last_non_vpn_cache() -> None:
    cached = CachedLocation(latitude=49.2, longitude=16.6, saved_at=100)

    for state in (VpnState.ACTIVE, VpnState.UNKNOWN):
        resolver = LocationResolver(
            LocationConfig(ip_lookup_ttl_seconds=60),
            vpn_status=lambda current=state: current,
            ip_lookup=lambda: (_ for _ in ()).throw(AssertionError("IP lookup called")),
            load_cache=lambda: cached,
            clock=lambda: 1000,
        )

        assert resolver.resolve() == Coordinates(49.2, 16.6, LocationSource.CACHE)


def test_inactive_vpn_refreshes_stale_ip_cache_and_saves_result() -> None:
    saved: list[CachedLocation] = []
    resolver = LocationResolver(
        LocationConfig(ip_lookup_ttl_seconds=60),
        vpn_status=lambda: VpnState.INACTIVE,
        ip_lookup=lambda: Coordinates(50.1, 14.4, LocationSource.IP),
        load_cache=lambda: CachedLocation(latitude=49.2, longitude=16.6, saved_at=100),
        save_cache=saved.append,
        clock=lambda: 1000,
    )

    assert resolver.resolve() == Coordinates(50.1, 14.4, LocationSource.IP)
    assert saved == [CachedLocation(latitude=50.1, longitude=14.4, saved_at=1000)]


def test_failed_ip_lookup_falls_back_to_timezone_city() -> None:
    resolver = LocationResolver(
        LocationConfig(),
        vpn_status=lambda: VpnState.INACTIVE,
        ip_lookup=lambda: None,
        timezone_lookup=lambda: Coordinates(50.0755, 14.4378, LocationSource.TIMEZONE),
    )

    assert resolver.resolve() == Coordinates(50.0755, 14.4378, LocationSource.TIMEZONE)


def _probe_name(command: tuple[str, ...]) -> str:
    if command[0] == "nmcli":
        return "nmcli"
    if "route" in command:
        return "route"
    return "link"
