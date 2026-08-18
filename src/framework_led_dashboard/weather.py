from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

from .http_json import JsonHttpClient
from .location import Coordinates


@dataclass(frozen=True, slots=True)
class WeatherReading:
    temperature_c: float
    weather_code: int
    is_day: bool

    def __post_init__(self) -> None:
        if isinstance(self.temperature_c, bool) or not isinstance(
            self.temperature_c, (int, float)
        ):
            raise TypeError("weather temperature must be a number")
        if not math.isfinite(self.temperature_c) or not -100 <= self.temperature_c <= 100:
            raise ValueError("weather temperature is outside the supported range")
        if isinstance(self.weather_code, bool) or not isinstance(self.weather_code, int):
            raise TypeError("weather code must be an integer")
        if not 0 <= self.weather_code <= 99:
            raise ValueError("weather code must be between 0 and 99")
        if not isinstance(self.is_day, bool):
            raise TypeError("is_day must be true or false")


@dataclass(frozen=True, slots=True)
class CachedWeather:
    reading: WeatherReading
    saved_at: float

    def __post_init__(self) -> None:
        if isinstance(self.saved_at, bool) or not isinstance(self.saved_at, (int, float)):
            raise TypeError("weather cache timestamp must be a number")
        if not math.isfinite(self.saved_at):
            raise ValueError("weather cache timestamp must be finite")


class OpenMeteoWeatherClient:
    def __init__(self, *, fetch_json: Callable[[str], object] | None = None) -> None:
        self._fetch_json = fetch_json or JsonHttpClient(
            allowed_hosts={"api.open-meteo.com"}
        ).get

    def fetch(self, location: Coordinates) -> WeatherReading | None:
        query = urlencode(
            {
                "latitude": location.latitude,
                "longitude": location.longitude,
                "current": "temperature_2m,weather_code,is_day",
            }
        )
        try:
            payload = self._fetch_json(f"https://api.open-meteo.com/v1/forecast?{query}")
            if not isinstance(payload, dict) or not isinstance(payload.get("current"), dict):
                return None
            current = payload["current"]
            return WeatherReading(
                temperature_c=_weather_number(current.get("temperature_2m")),
                weather_code=_weather_code(current.get("weather_code")),
                is_day=_daylight(current.get("is_day")),
            )
        except (OSError, TypeError, ValueError):
            return None


class WeatherProvider:
    def __init__(
        self,
        *,
        fetch_weather: Callable[[Coordinates], WeatherReading | None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetch_weather = fetch_weather
        self._clock = clock
        self._last_good: CachedWeather | None = None

    def refresh(self, location: Coordinates) -> CachedWeather | None:
        try:
            reading = self._fetch_weather(location)
        except (OSError, TypeError, ValueError):
            reading = None
        if reading is not None:
            self._last_good = CachedWeather(reading=reading, saved_at=self._clock())
        return self._last_good

    def current(self) -> CachedWeather | None:
        return self._last_good


def _weather_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("weather temperature must be a number")
    return float(value)


def _weather_code(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("weather code must be an integer")
    return value


def _daylight(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1}:
        raise TypeError("is_day must be integer 0 or 1")
    return value == 1
