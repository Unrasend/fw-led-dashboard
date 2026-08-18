from __future__ import annotations

import math
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .config import LocationConfig

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
