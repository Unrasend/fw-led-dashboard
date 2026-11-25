from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlencode

from .config import LocationConfig
from .http_json import JsonHttpClient

CommandRunner = Callable[[tuple[str, ...]], str]


class VpnState(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class LocationSource(Enum):
    EXPLICIT = "explicit"
    IP = "ip"
    CACHE = "cache"
    TIMEZONE = "timezone"


@dataclass(frozen=True, slots=True)
class Coordinates:
    latitude: float
    longitude: float
    source: LocationSource

    def __post_init__(self) -> None:
        if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ValueError("latitude must be finite and between -90 and 90")
        if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ValueError("longitude must be finite and between -180 and 180")


@dataclass(frozen=True, slots=True)
class CachedLocation:
    latitude: float
    longitude: float
    saved_at: float

    def __post_init__(self) -> None:
        Coordinates(self.latitude, self.longitude, LocationSource.CACHE)
        if isinstance(self.saved_at, bool) or not isinstance(self.saved_at, (int, float)):
            raise TypeError("saved_at must be a finite number")
        if not math.isfinite(self.saved_at):
            raise ValueError("saved_at must be a finite number")


class IpLocationClient:
    def __init__(self, *, fetch_json: Callable[[str], object] | None = None) -> None:
        self._fetch_json = fetch_json or JsonHttpClient(allowed_hosts={"ipwho.is"}).get

    def lookup(self) -> Coordinates | None:
        query = urlencode({"fields": "success,latitude,longitude"})
        try:
            payload = self._fetch_json(f"https://ipwho.is/?{query}")
            if not isinstance(payload, dict) or payload.get("success") is not True:
                return None
            return Coordinates(
                _external_number(payload.get("latitude"), "latitude"),
                _external_number(payload.get("longitude"), "longitude"),
                LocationSource.IP,
            )
        except (OSError, TypeError, ValueError):
            return None


class OpenMeteoGeocoder:
    def __init__(self, *, fetch_json: Callable[[str], object] | None = None) -> None:
        self._fetch_json = fetch_json or JsonHttpClient(
            allowed_hosts={"geocoding-api.open-meteo.com"}
        ).get

    def lookup_city(self, city: str | None) -> Coordinates | None:
        if city is None or len(city.strip()) < 2:
            return None
        query = urlencode({"name": city.strip(), "count": 1, "language": "en", "format": "json"})
        try:
            payload = self._fetch_json(
                f"https://geocoding-api.open-meteo.com/v1/search?{query}"
            )
            if not isinstance(payload, dict):
                return None
            results = payload.get("results")
            if not isinstance(results, list) or not results or not isinstance(results[0], dict):
                return None
            return Coordinates(
                _external_number(results[0].get("latitude"), "latitude"),
                _external_number(results[0].get("longitude"), "longitude"),
                LocationSource.TIMEZONE,
            )
        except (OSError, TypeError, ValueError):
            return None


class JsonLocationCache:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> CachedLocation | None:
        try:
            with self._path.open("rb") as cache_file:
                body = cache_file.read(4097)
        except FileNotFoundError:
            return None
        if len(body) > 4096:
            raise ValueError("location cache is too large")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise TypeError
            return CachedLocation(
                _external_number(payload.get("latitude"), "latitude"),
                _external_number(payload.get("longitude"), "longitude"),
                _external_number(payload.get("saved_at"), "saved_at"),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError("invalid location cache") from error

    def save(self, location: CachedLocation) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(
                {
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "saved_at": location.saved_at,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(self._path)


def timezone_city(
    *,
    timezone_file: str | Path = "/etc/timezone",
    localtime_file: str | Path = "/etc/localtime",
    environ: Mapping[str, str] = os.environ,
) -> str | None:
    # A zone name in TZ first: the Docker variant passes the desktop's zone there (a
    # container's /etc/localtime is not the desktop's). A path in TZ is left to the files.
    zone = environ.get("TZ", "").lstrip(":")
    timezone_name: str | None = zone if zone and not zone.startswith("/") else None
    try:
        if timezone_name is None:
            timezone_name = Path(timezone_file).read_text(encoding="utf-8").strip()
    except OSError:
        try:
            resolved = Path(localtime_file).resolve(strict=True)
            marker = resolved.parts.index("zoneinfo")
            timezone_name = "/".join(resolved.parts[marker + 1 :])
        except (OSError, ValueError):
            return None
    if not timezone_name or "/" not in timezone_name:
        return None
    city = timezone_name.rsplit("/", 1)[-1].replace("_", " ").strip()
    return city if len(city) >= 2 else None


class VpnDetector:
    def __init__(
        self,
        *,
        interface_prefixes: tuple[str, ...] = LocationConfig().vpn_interface_prefixes,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._prefixes = tuple(prefix.lower() for prefix in interface_prefixes)
        self._run = command_runner or _run_command

    def status(self) -> VpnState:
        probes = (
            (
                ("nmcli", "--terse", "--fields", "TYPE,DEVICE", "connection", "show", "--active"),
                self._networkmanager_has_vpn,
            ),
            (("ip", "-o", "route", "show", "default"), self._default_route_has_vpn),
            (("ip", "-o", "link", "show", "up"), self._active_link_has_vpn),
        )
        for command, detects_vpn in probes:
            try:
                output = self._run(command)
            except (OSError, subprocess.SubprocessError):
                return VpnState.UNKNOWN
            if detects_vpn(output):
                return VpnState.ACTIVE
        return VpnState.INACTIVE

    def _networkmanager_has_vpn(self, output: str) -> bool:
        for line in output.splitlines():
            connection_type, _, device = line.partition(":")
            if connection_type.lower() in {"vpn", "wireguard"}:
                return True
            if self._vpn_interface(device):
                return True
        return False

    def _default_route_has_vpn(self, output: str) -> bool:
        return any(
            self._vpn_interface(match.group(1))
            for match in re.finditer(r"\bdev\s+(\S+)", output)
        )

    def _active_link_has_vpn(self, output: str) -> bool:
        for line in output.splitlines():
            match = re.match(r"^\d+:\s+([^:@]+)(?:@[^:]+)?:", line)
            if match is not None and self._vpn_interface(match.group(1)):
                return True
        return False

    def _vpn_interface(self, interface: str) -> bool:
        normalized = interface.strip().lower()
        return bool(normalized) and normalized.startswith(self._prefixes)


class LocationResolver:
    def __init__(
        self,
        config: LocationConfig,
        *,
        vpn_status: Callable[[], VpnState],
        ip_lookup: Callable[[], Coordinates | None],
        load_cache: Callable[[], CachedLocation | None] = lambda: None,
        save_cache: Callable[[CachedLocation], None] = lambda _location: None,
        timezone_lookup: Callable[[], Coordinates | None] = lambda: None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._vpn_status = vpn_status
        self._ip_lookup = ip_lookup
        self._load_cache = load_cache
        self._save_cache = save_cache
        self._timezone_lookup = timezone_lookup
        self._clock = clock

    def resolve(self) -> Coordinates | None:
        if self._config.latitude is not None and self._config.longitude is not None:
            return Coordinates(
                self._config.latitude,
                self._config.longitude,
                LocationSource.EXPLICIT,
            )

        cached = self._safe_load_cache()
        now = self._clock()
        if self._config.ip_lookup_enabled and self._vpn_status() is VpnState.INACTIVE:
            if cached is not None and 0 <= now - cached.saved_at < self._config.ip_lookup_ttl_seconds:
                return Coordinates(cached.latitude, cached.longitude, LocationSource.CACHE)
            location = self._safe_ip_lookup()
            if location is not None:
                saved = CachedLocation(location.latitude, location.longitude, now)
                try:
                    self._save_cache(saved)
                except OSError:
                    pass
                return Coordinates(location.latitude, location.longitude, LocationSource.IP)

        if cached is not None:
            try:
                return Coordinates(cached.latitude, cached.longitude, LocationSource.CACHE)
            except ValueError:
                pass
        try:
            return self._timezone_lookup()
        except (OSError, ValueError):
            return None

    def _safe_load_cache(self) -> CachedLocation | None:
        try:
            return self._load_cache()
        except (OSError, ValueError):
            return None

    def _safe_ip_lookup(self) -> Coordinates | None:
        try:
            return self._ip_lookup()
        except (OSError, ValueError):
            return None


def _run_command(command: tuple[str, ...]) -> str:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return result.stdout


def _external_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number
