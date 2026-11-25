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
from framework_led_dashboard.right_panel import render_right_panel
from framework_led_dashboard.weather import (
    CachedWeather,
    OpenMeteoWeatherClient,
    WeatherProvider,
    WeatherReading,
)
from framework_led_dashboard.weather_codes import (
    SUPPORTED_WEATHER_CODES,
    WeatherIntensity,
    WeatherKind,
    WeatherSymbol,
    classify_weather_code,
)
from framework_led_dashboard.weather_icons import (
    BIG_FLAKE,
    BIG_MOON,
    FLAKE,
    ICON_FRAMES,
    ICON_HEIGHT,
    ICON_INTENSITIES,
    ICON_WIDTH,
    MOON,
    Scene,
    WeatherIconArtist,
    weather_icon,
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

    city = timezone_city(
        timezone_file=timezone_file, localtime_file=tmp_path / "missing", environ={}
    )
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


def test_open_meteo_client_requests_current_temperature_weather_code_and_daylight() -> None:
    requested_urls: list[str] = []

    def fetch_json(url: str) -> object:
        requested_urls.append(url)
        return {"current": {"temperature_2m": 18.4, "weather_code": 61, "is_day": 0}}

    location = Coordinates(50.0755, 14.4378, LocationSource.IP)

    assert OpenMeteoWeatherClient(fetch_json=fetch_json).fetch(location) == WeatherReading(
        temperature_c=18.4,
        weather_code=61,
        is_day=False,
    )
    parsed = urlsplit(requested_urls[0])
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "https",
        "api.open-meteo.com",
        "/v1/forecast",
    )
    assert parse_qs(parsed.query) == {
        "latitude": ["50.0755"],
        "longitude": ["14.4378"],
        "current": ["temperature_2m,weather_code,is_day"],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"current": []},
        {"current": {"temperature_2m": True, "weather_code": 0, "is_day": 1}},
        {"current": {"temperature_2m": 18, "weather_code": True, "is_day": 1}},
        {"current": {"temperature_2m": 18, "weather_code": 4, "is_day": 1}},
        {"current": {"temperature_2m": 18, "weather_code": 100, "is_day": 1}},
        {"current": {"temperature_2m": float("nan"), "weather_code": 0, "is_day": 1}},
        {"current": {"temperature_2m": 18, "weather_code": 0}},
        {"current": {"temperature_2m": 18, "weather_code": 0, "is_day": True}},
        {"current": {"temperature_2m": 18, "weather_code": 0, "is_day": 2}},
    ],
)
def test_open_meteo_client_rejects_malformed_responses(payload: object) -> None:
    location = Coordinates(50.0755, 14.4378, LocationSource.CACHE)

    assert OpenMeteoWeatherClient(fetch_json=lambda _url: payload).fetch(location) is None


def test_weather_provider_returns_last_good_reading_during_outage() -> None:
    responses: list[WeatherReading | None] = [WeatherReading(18.4, 61, False), None]
    times = iter((1000.0, 2000.0))
    provider = WeatherProvider(fetch_weather=lambda _location: responses.pop(0), clock=times.__next__)
    location = Coordinates(50.0755, 14.4378, LocationSource.IP)

    first = provider.refresh(location)
    second = provider.refresh(location)

    assert first == CachedWeather(WeatherReading(18.4, 61, False), saved_at=1000.0)
    assert second is first
    assert provider.current() is first


def test_weather_provider_contains_fetch_timeout() -> None:
    def timeout(_location: Coordinates) -> WeatherReading | None:
        raise TimeoutError("Open-Meteo timed out")

    provider = WeatherProvider(fetch_weather=timeout)

    assert provider.refresh(Coordinates(50.0755, 14.4378, LocationSource.TIMEZONE)) is None


WMO_SYMBOLS = {
    0: WeatherSymbol(WeatherKind.CLEAR),
    1: WeatherSymbol(WeatherKind.MAINLY_CLEAR),
    2: WeatherSymbol(WeatherKind.PARTLY_CLOUDY),
    3: WeatherSymbol(WeatherKind.OVERCAST),
    45: WeatherSymbol(WeatherKind.FOG),
    48: WeatherSymbol(WeatherKind.RIME_FOG),
    51: WeatherSymbol(WeatherKind.DRIZZLE, WeatherIntensity.LIGHT),
    53: WeatherSymbol(WeatherKind.DRIZZLE, WeatherIntensity.MODERATE),
    55: WeatherSymbol(WeatherKind.DRIZZLE, WeatherIntensity.HEAVY),
    56: WeatherSymbol(WeatherKind.FREEZING_DRIZZLE, WeatherIntensity.LIGHT),
    57: WeatherSymbol(WeatherKind.FREEZING_DRIZZLE, WeatherIntensity.HEAVY),
    61: WeatherSymbol(WeatherKind.RAIN, WeatherIntensity.LIGHT),
    63: WeatherSymbol(WeatherKind.RAIN, WeatherIntensity.MODERATE),
    65: WeatherSymbol(WeatherKind.RAIN, WeatherIntensity.HEAVY),
    66: WeatherSymbol(WeatherKind.FREEZING_RAIN, WeatherIntensity.LIGHT),
    67: WeatherSymbol(WeatherKind.FREEZING_RAIN, WeatherIntensity.HEAVY),
    71: WeatherSymbol(WeatherKind.SNOW, WeatherIntensity.LIGHT),
    73: WeatherSymbol(WeatherKind.SNOW, WeatherIntensity.MODERATE),
    75: WeatherSymbol(WeatherKind.SNOW, WeatherIntensity.HEAVY),
    77: WeatherSymbol(WeatherKind.SNOW_GRAINS),
    80: WeatherSymbol(WeatherKind.RAIN_SHOWERS, WeatherIntensity.LIGHT),
    81: WeatherSymbol(WeatherKind.RAIN_SHOWERS, WeatherIntensity.MODERATE),
    82: WeatherSymbol(WeatherKind.RAIN_SHOWERS, WeatherIntensity.HEAVY),
    85: WeatherSymbol(WeatherKind.SNOW_SHOWERS, WeatherIntensity.LIGHT),
    86: WeatherSymbol(WeatherKind.SNOW_SHOWERS, WeatherIntensity.HEAVY),
    95: WeatherSymbol(WeatherKind.THUNDER),
    96: WeatherSymbol(WeatherKind.THUNDER_HAIL, WeatherIntensity.LIGHT),
    99: WeatherSymbol(WeatherKind.THUNDER_HAIL, WeatherIntensity.HEAVY),
}


@pytest.mark.parametrize(("weather_code", "symbol"), WMO_SYMBOLS.items())
def test_every_documented_wmo_code_maps_to_exact_display_symbol(
    weather_code: int,
    symbol: WeatherSymbol,
) -> None:
    assert classify_weather_code(weather_code) == symbol


def test_supported_weather_codes_exactly_match_open_meteo_documentation() -> None:
    assert SUPPORTED_WEATHER_CODES == frozenset(WMO_SYMBOLS)


@pytest.mark.parametrize("weather_code", [-1, 4, 44, 49, 50, 58, 100])
def test_undocumented_wmo_codes_are_rejected(weather_code: int) -> None:
    with pytest.raises(ValueError, match="unsupported weather code"):
        classify_weather_code(weather_code)
    with pytest.raises(ValueError, match="unsupported weather code"):
        WeatherReading(18.0, weather_code, True)


DISPLAY_SYMBOLS = tuple(dict.fromkeys(WMO_SYMBOLS.values()))


@pytest.mark.parametrize("symbol", DISPLAY_SYMBOLS)
@pytest.mark.parametrize("is_day", [True, False])
def test_every_weather_symbol_has_a_deterministic_nine_by_ten_twenty_frame_cycle(
    symbol: WeatherSymbol,
    is_day: bool,
) -> None:
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]

    assert ICON_WIDTH == 9 and ICON_HEIGHT == 10 and ICON_FRAMES == 20
    assert all(len(icon) == ICON_HEIGHT for icon in cycle)
    assert all(len(row) == ICON_WIDTH for icon in cycle for row in icon)
    assert set("".join(row for icon in cycle for row in icon)) <= {".", "L", "#"}
    assert len(set(cycle)) > 1
    assert weather_icon(symbol, is_day=is_day, frame_index=ICON_FRAMES) == cycle[0]


@pytest.mark.parametrize("symbol", DISPLAY_SYMBOLS)
def test_every_weather_symbol_has_distinct_day_and_night_art(symbol: WeatherSymbol) -> None:
    day = weather_icon(symbol, is_day=True, frame_index=0)
    night = weather_icon(symbol, is_day=False, frame_index=0)

    assert day != night


@pytest.mark.parametrize("symbol", DISPLAY_SYMBOLS)
@pytest.mark.parametrize("is_day", [True, False])
def test_every_animation_loops_without_a_seam(symbol: WeatherSymbol, is_day: bool) -> None:
    """The icon starts over after ICON_FRAMES: what it would draw next must be its first frames."""

    def unwrapped(index: int) -> tuple[str, ...]:
        return WeatherIconArtist(Scene(symbol, is_day, index)).draw()

    following = [unwrapped(ICON_FRAMES + index) for index in range(ICON_FRAMES)]
    assert following == [unwrapped(index) for index in range(ICON_FRAMES)]


def _lit(icon: tuple[str, ...], x: int, y: int) -> bool:
    return 0 <= x < ICON_WIDTH and 0 <= y < ICON_HEIGHT and icon[y][x] in {"L", "#"}


def _shows(icon: tuple[str, ...], art: tuple[str, ...], x0: int, y0: int) -> bool:
    """The glyph fills its box exactly: every pixel of it lit, every gap dark."""
    return all(
        _lit(icon, x0 + dx, y0 + dy) == (char != ".")
        for dy, row in enumerate(art)
        for dx, char in enumerate(row)
    )


def _shows_anywhere(icon: tuple[str, ...], art: tuple[str, ...]) -> bool:
    return any(_shows(icon, art, x, y) for y in range(ICON_HEIGHT) for x in range(ICON_WIDTH))


@pytest.mark.parametrize("symbol", DISPLAY_SYMBOLS)
def test_every_night_icon_shows_a_crescent_moon(symbol: WeatherSymbol) -> None:
    for index in range(ICON_FRAMES):
        night = weather_icon(symbol, is_day=False, frame_index=index)

        assert _shows_anywhere(night, MOON) or _shows_anywhere(night, BIG_MOON)


@pytest.mark.parametrize("weather_code", [0, 1, 2])
def test_blinking_stars_never_touch_the_moon_or_a_cloud(weather_code: int) -> None:
    symbol = classify_weather_code(weather_code)
    cycle = [weather_icon(symbol, is_day=False, frame_index=index) for index in range(ICON_FRAMES)]
    cells = [(x, y) for y in range(ICON_HEIGHT) for x in range(ICON_WIDTH)]
    steady = {(x, y) for x, y in cells if all(_lit(icon, x, y) for icon in cycle)}
    stars = {(x, y) for x, y in cells if any(_lit(icon, x, y) for icon in cycle)} - steady

    assert stars
    assert all(max(abs(x - sx), abs(y - sy)) >= 2 for x, y in stars for sx, sy in steady)


@pytest.mark.parametrize("is_day", [True, False])
def test_partly_cloudy_shows_nothing_under_its_cloud(is_day: bool) -> None:
    """Anything below the cloud would read as precipitation."""
    symbol = classify_weather_code(2)
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]

    assert all(row == "." * ICON_WIDTH for icon in cycle for row in icon[7:])


def test_the_sun_turns_its_rays_once_a_second() -> None:
    """At two frames a second: a sun that flips on every frame flickers at the edge of sight."""
    symbol = classify_weather_code(0)
    cycle = [weather_icon(symbol, is_day=True, frame_index=index) for index in range(ICON_FRAMES)]

    assert cycle[0] == cycle[1] != cycle[2] == cycle[3] != cycle[4]


def test_the_dim_level_is_far_below_the_bright_one() -> None:
    """Drizzle against rain and snow grains against snow differ only by this level."""
    assert ICON_INTENSITIES["L"] * 3 <= ICON_INTENSITIES["#"]


@pytest.mark.parametrize(
    ("light_code", "heavy_code"),
    [(51, 55), (56, 57), (61, 65), (66, 67), (71, 75), (80, 82), (85, 86), (96, 99)],
)
def test_light_and_heavy_weather_have_different_art(light_code: int, heavy_code: int) -> None:
    light = [weather_icon(classify_weather_code(light_code), is_day=True, frame_index=i) for i in range(4)]
    heavy = [weather_icon(classify_weather_code(heavy_code), is_day=True, frame_index=i) for i in range(4)]

    assert light != heavy


SINGLE_DROP_CODES = (51, 53, 56)  # light and moderate drizzle
STROKE_CODES = (55, 57, 61, 63, 65, 66, 67, 80, 81, 82)  # dense drizzle and every rain
FREEZING_CODES = (48, 56, 57, 66, 67)  # rime fog, freezing drizzle, freezing rain
FLAKE_CORNER = (6, 7)
SNOW_CODES = (71, 73, 75, 85, 86)
SNOW_GRAINS_CODE = 77
THUNDER_CODES = (95, 96, 99)


def _falling(icon: tuple[str, ...], weather_code: int) -> dict[tuple[int, int], str]:
    """What falls from the cloud: the lit pixels of the bottom rows, the snowflake aside."""
    flake_x, flake_y = FLAKE_CORNER
    return {
        (x, y): icon[y][x]
        for y in range(5, ICON_HEIGHT)
        for x in range(ICON_WIDTH)
        if _lit(icon, x, y)
        and not (weather_code in FREEZING_CODES and x >= flake_x - 1 and y >= flake_y - 1)
    }


def _down_left_strokes(icon: tuple[str, ...]) -> set[tuple[int, int]]:
    def continues(x: int, y: int) -> bool:
        return _lit(icon, x - 1, y + 1) or _lit(icon, x - 2, y + 2)

    return {(x, y) for y in range(5, 9) for x in range(1, 9) if _lit(icon, x, y) and continues(x, y)}


@pytest.mark.parametrize("weather_code", SINGLE_DROP_CODES)
@pytest.mark.parametrize("is_day", [True, False])
def test_drizzle_falls_as_single_drops_moving_down_left(weather_code: int, is_day: bool) -> None:
    symbol = classify_weather_code(weather_code)
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]
    drops = [set(_falling(icon, weather_code)) for icon in cycle]

    assert all(drops)
    assert not any((x - 1, y + 1) in frame_drops for frame_drops in drops for x, y in frame_drops)
    moved = 0
    for now, later in zip(drops, drops[1:] + drops[:1], strict=True):
        if later != now:
            moved += 1
            assert {(x - 1, y + 1) for x, y in now if x > 0 and y < ICON_HEIGHT - 1} <= later
    assert moved


@pytest.mark.parametrize("weather_code", SINGLE_DROP_CODES + STROKE_CODES)
@pytest.mark.parametrize("is_day", [True, False])
def test_drizzle_is_dim_and_rain_is_bright(weather_code: int, is_day: bool) -> None:
    symbol = classify_weather_code(weather_code)
    drizzle = symbol.kind in {WeatherKind.DRIZZLE, WeatherKind.FREEZING_DRIZZLE}
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]

    levels = {level for icon in cycle for level in _falling(icon, weather_code).values()}
    assert levels == ({"L"} if drizzle else {"#"})


def _corner_flake(icon: tuple[str, ...]) -> str | None:
    """"open" or "filled" when the bottom right corner holds the small snowflake, else None.

    The crystal itself is bright, its gaps are dark or all dim, and one pixel of darkness
    around the glyph keeps it apart from the precipitation."""
    x0, y0 = FLAKE_CORNER
    crystal = {icon[y0 + dy][x0 + dx] for dy, row in enumerate(FLAKE) for dx, c in enumerate(row) if c != "."}
    gaps = {icon[y0 + dy][x0 + dx] for dy, row in enumerate(FLAKE) for dx, c in enumerate(row) if c == "."}
    margin = {icon[y0 - 1][x] for x in range(x0 - 1, ICON_WIDTH)} | {
        icon[y][x0 - 1] for y in range(y0, ICON_HEIGHT)
    }
    if crystal != {"#"} or margin != {"."} or gaps not in ({"."}, {"L"}):
        return None
    return "open" if gaps == {"."} else "filled"


@pytest.mark.parametrize("weather_code", sorted(SUPPORTED_WEATHER_CODES))
@pytest.mark.parametrize("is_day", [True, False])
def test_only_freezing_weather_carries_a_glinting_snowflake_in_the_bottom_right_corner(
    weather_code: int, is_day: bool
) -> None:
    symbol = classify_weather_code(weather_code)
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]

    states = {_corner_flake(icon) for icon in cycle}
    assert states == ({"open", "filled"} if weather_code in FREEZING_CODES else {None})


@pytest.mark.parametrize("weather_code", STROKE_CODES)
@pytest.mark.parametrize("is_day", [True, False])
def test_rain_falls_as_moving_down_left_strokes(weather_code: int, is_day: bool) -> None:
    symbol = classify_weather_code(weather_code)
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]
    strokes = [_down_left_strokes(icon) for icon in cycle]

    assert all(strokes)
    assert len({frozenset(stroke) for stroke in strokes}) > 1


def _snowflake_x(icon: tuple[str, ...]) -> int | None:
    """The left edge of the five-by-five snowflake under the cloud."""
    return next((x for x in range(ICON_WIDTH - 4) if _shows(icon, BIG_FLAKE, x, 5)), None)


@pytest.mark.parametrize("weather_code", SNOW_CODES)
@pytest.mark.parametrize("is_day", [True, False])
def test_snow_shows_a_twinkling_snowflake_under_the_cloud(weather_code: int, is_day: bool) -> None:
    symbol = classify_weather_code(weather_code)
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]

    places = {_snowflake_x(icon) for icon in cycle}
    assert len(places) == 1 and None not in places
    flake_x = places.pop()
    assert {icon[5][flake_x + 2] for icon in cycle} == {"L", "#"}  # the tips dim and light up
    assert {icon[7][flake_x + 2] for icon in cycle} == {"#"}  # the heart stays bright


@pytest.mark.parametrize("weather_code", SNOW_CODES)
@pytest.mark.parametrize("is_day", [True, False])
def test_single_flakes_fall_straight_down_beside_the_snowflake(weather_code: int, is_day: bool) -> None:
    symbol = classify_weather_code(weather_code)
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]
    flake_x = _snowflake_x(cycle[0])
    assert flake_x is not None
    beside = [
        {
            (x, y)
            for y in range(5, ICON_HEIGHT)
            for x in range(ICON_WIDTH)
            if _lit(icon, x, y) and not flake_x <= x < flake_x + 5
        }
        for icon in cycle
    ]

    assert all(beside)
    assert all(x < flake_x - 1 or x > flake_x + 5 for flakes in beside for x, _y in flakes)
    moved = 0
    for now, later in zip(beside, beside[1:] + beside[:1], strict=True):
        if later != now:
            moved += 1
            assert {(x, 5 + (y - 4) % 5) for x, y in now} == later  # one row down, around the bottom
    assert moved


@pytest.mark.parametrize(("light_code", "heavy_code"), [(71, 73), (73, 75), (85, 86)])
def test_heavier_snow_has_more_single_flakes(light_code: int, heavy_code: int) -> None:
    def lit(weather_code: int) -> int:
        icon = weather_icon(classify_weather_code(weather_code), is_day=True, frame_index=0)
        return sum(_lit(icon, x, y) for y in range(5, ICON_HEIGHT) for x in range(ICON_WIDTH))

    assert lit(light_code) < lit(heavy_code)


@pytest.mark.parametrize("is_day", [True, False])
def test_snow_grains_fall_straight_down_as_dim_dots_in_three_columns(is_day: bool) -> None:
    symbol = classify_weather_code(SNOW_GRAINS_CODE)
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(ICON_FRAMES)]

    grains = [
        {(x, y) for y in range(5, ICON_HEIGHT) for x in range(ICON_WIDTH) if _lit(icon, x, y)}
        for icon in cycle
    ]
    assert {icon[y][x] for icon, frame in zip(cycle, grains, strict=True) for x, y in frame} == {"L"}
    assert all(len({x for x, _y in frame}) == 3 for frame in grains)
    assert grains[0] != grains[1]
    assert {x for x, _y in grains[0]} == {x for x, _y in grains[1]}
    assert all(_snowflake_x(icon) is None for icon in cycle)


@pytest.mark.parametrize("weather_code", THUNDER_CODES)
@pytest.mark.parametrize("is_day", [True, False])
def test_thunder_shows_a_flashing_bolt(weather_code: int, is_day: bool) -> None:
    symbol = classify_weather_code(weather_code)
    cycle = [weather_icon(symbol, is_day=is_day, frame_index=index) for index in range(4)]

    lit_counts = [sum(row.count("#") for row in icon[5:10]) for icon in cycle]
    assert max(lit_counts) > min(lit_counts)


@pytest.mark.parametrize("weather_code", sorted(SUPPORTED_WEATHER_CODES))
def test_right_panel_renders_every_documented_weather_code(weather_code: int) -> None:
    frame = render_right_panel(
        weather=WeatherReading(18.4, weather_code, False),
        weather_frame=1,
        cpu_temperature_c=54,
    ).snapshot()

    assert any(pixel for row in frame[0:10] for pixel in row)
    assert frame[10] == (0,) * 9


def test_right_panel_uses_daylight_state_for_weather_art() -> None:
    common = {"weather_frame": 0, "cpu_temperature_c": 54}

    day = render_right_panel(weather=WeatherReading(18.4, 2, True), **common).snapshot()
    night = render_right_panel(weather=WeatherReading(18.4, 2, False), **common).snapshot()

    assert day[0:10] != night[0:10]


def test_right_panel_renders_weather_icon_and_outside_temperature_in_assigned_rows() -> None:
    frame = render_right_panel(
        weather=WeatherReading(18.4, 0, True),
        weather_frame=0,
        cpu_temperature_c=54,
    ).snapshot()

    assert tuple("".join("#" if pixel else "." for pixel in row) for row in frame[11:16]) == (
        ".#.###..#",
        "##.#.#...",
        ".#.###...",
        ".#.#.#...",
        "######...",
    )
    assert all(pixel == 0 for row in frame[10:11] + frame[16:17] for pixel in row)
    assert any(pixel for row in frame[0:10] for pixel in row)


def test_right_panel_renders_unknown_weather_temperature_dim_without_icon() -> None:
    frame = render_right_panel(weather=None, cpu_temperature_c=None).snapshot()

    assert all(pixel == 0 for row in frame[0:10] for pixel in row)
    assert frame[13] == (0, 24, 24, 24, 0, 24, 24, 24, 0)


def _probe_name(command: tuple[str, ...]) -> str:
    if command[0] == "nmcli":
        return "nmcli"
    if "route" in command:
        return "route"
    return "link"


@pytest.mark.parametrize(
    ("tz", "city"),
    [("Europe/Prague", "Prague"), (":America/New_York", "New York"), ("UTC", None), ("", "Berlin"),
     ("/usr/share/zoneinfo/Asia/Tokyo", "Berlin")],
)
def test_timezone_city_prefers_a_zone_named_in_tz(tmp_path: Path, tz: str, city: str | None) -> None:
    timezone_file = tmp_path / "timezone"
    timezone_file.write_text("Europe/Berlin\n", encoding="utf-8")

    found = timezone_city(
        timezone_file=timezone_file, localtime_file=tmp_path / "missing", environ={"TZ": tz}
    )

    assert found == city
