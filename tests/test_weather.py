from __future__ import annotations

from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, urlsplit

import pytest

from framework_led_dashboard.config import LocationConfig
from framework_led_dashboard.http_json import JsonHttpClient
from framework_led_dashboard.location import (
    CachedLocation,
    Coordinates,
    IpLocationClient,
    JsonLocationCache,
    LocationResolver,
    LocationSource,
    OpenMeteoGeocoder,
    VpnDetector,
    VpnState,
    timezone_city,
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


def test_ip_location_client_requests_only_coordinates() -> None:
    requested_urls: list[str] = []

    def fetch_json(url: str) -> object:
        requested_urls.append(url)
        return {"success": True, "latitude": 50.0755, "longitude": 14.4378}

    assert IpLocationClient(fetch_json=fetch_json).lookup() == Coordinates(
        50.0755,
        14.4378,
        LocationSource.IP,
    )
    parsed = urlsplit(requested_urls[0])
    assert (parsed.scheme, parsed.hostname, parsed.path) == ("https", "ipwho.is", "/")
    assert parse_qs(parsed.query) == {"fields": ["success,latitude,longitude"]}


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "latitude": 50.0, "longitude": 14.0},
        {"success": True, "latitude": True, "longitude": 14.0},
        {"success": True, "latitude": 91.0, "longitude": 14.0},
        {"success": True, "latitude": 50.0},
        [],
    ],
)
def test_ip_location_client_rejects_malformed_responses(payload: object) -> None:
    assert IpLocationClient(fetch_json=lambda _url: payload).lookup() is None


def test_ip_location_client_treats_timeout_as_unavailable() -> None:
    def timeout(_url: str) -> object:
        raise TimeoutError("IP service timed out")

    assert IpLocationClient(fetch_json=timeout).lookup() is None


def test_json_location_cache_round_trips_private_file(tmp_path: Path) -> None:
    path = tmp_path / "dashboard" / "location.json"
    cache = JsonLocationCache(path)

    cache.save(CachedLocation(latitude=50.0755, longitude=14.4378, saved_at=1234.5))

    assert cache.load() == CachedLocation(50.0755, 14.4378, 1234.5)
    assert path.stat().st_mode & 0o777 == 0o600


def test_json_location_cache_rejects_untrusted_payload(tmp_path: Path) -> None:
    path = tmp_path / "location.json"
    path.write_text('{"latitude": true, "longitude": 14, "saved_at": 1}', encoding="utf-8")

    with pytest.raises(ValueError, match="location cache"):
        JsonLocationCache(path).load()


def test_timezone_city_is_geocoded_with_open_meteo(tmp_path: Path) -> None:
    timezone_file = tmp_path / "timezone"
    timezone_file.write_text("Europe/Prague\n", encoding="utf-8")
    requested_urls: list[str] = []

    def fetch_json(url: str) -> object:
        requested_urls.append(url)
        return {"results": [{"latitude": 50.0755, "longitude": 14.4378}]}

    city = timezone_city(timezone_file=timezone_file, localtime_file=tmp_path / "missing")
    location = OpenMeteoGeocoder(fetch_json=fetch_json).lookup_city(city)

    assert city == "Prague"
    assert location == Coordinates(50.0755, 14.4378, LocationSource.TIMEZONE)
    parsed = urlsplit(requested_urls[0])
    assert parsed.hostname == "geocoding-api.open-meteo.com"
    assert parse_qs(parsed.query) == {
        "name": ["Prague"],
        "count": ["1"],
        "language": ["en"],
        "format": ["json"],
    }


class _HttpResponse:
    def __init__(self, body: bytes, url: str = "https://ipwho.is/") -> None:
        self._body = body
        self._url = url

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._body[:size]

    def geturl(self) -> str:
        return self._url


def test_json_http_client_enforces_https_host_timeout_and_body_limit() -> None:
    calls: list[tuple[str, float]] = []

    def open_response(request: object, *, timeout: float) -> _HttpResponse:
        calls.append((request.full_url, timeout))  # type: ignore[attr-defined]
        return _HttpResponse(b'{"success": true}')

    client = JsonHttpClient(allowed_hosts={"ipwho.is"}, opener=open_response)

    assert client.get("https://ipwho.is/") == {"success": True}
    assert calls == [("https://ipwho.is/", 4.0)]
    with pytest.raises(ValueError, match="HTTPS host"):
        client.get("http://ipwho.is/")

    oversized = JsonHttpClient(
        allowed_hosts={"ipwho.is"},
        opener=lambda _request, timeout: _HttpResponse(b"x" * 32769),
    )
    with pytest.raises(ValueError, match="too large"):
        oversized.get("https://ipwho.is/")


def _probe_name(command: tuple[str, ...]) -> str:
    if command[0] == "nmcli":
        return "nmcli"
    if "route" in command:
        return "route"
    return "link"
