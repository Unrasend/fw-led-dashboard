from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WeatherKind(Enum):
    CLEAR = "clear"
    MAINLY_CLEAR = "mainly_clear"
    PARTLY_CLOUDY = "partly_cloudy"
    OVERCAST = "overcast"
    FOG = "fog"
    RIME_FOG = "rime_fog"
    DRIZZLE = "drizzle"
    FREEZING_DRIZZLE = "freezing_drizzle"
    RAIN = "rain"
    FREEZING_RAIN = "freezing_rain"
    SNOW = "snow"
    SNOW_GRAINS = "snow_grains"
    RAIN_SHOWERS = "rain_showers"
    SNOW_SHOWERS = "snow_showers"
    THUNDER = "thunder"
    THUNDER_HAIL = "thunder_hail"


class WeatherIntensity(Enum):
    STANDARD = "standard"
    LIGHT = "light"
    MODERATE = "moderate"
    HEAVY = "heavy"


@dataclass(frozen=True, slots=True)
class WeatherSymbol:
    kind: WeatherKind
    intensity: WeatherIntensity = WeatherIntensity.STANDARD


# Open-Meteo WMO interpretation table:
# https://open-meteo.com/en/docs#weathervariables
_WMO_SYMBOLS = {
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
SUPPORTED_WEATHER_CODES = frozenset(_WMO_SYMBOLS)


def classify_weather_code(weather_code: int) -> WeatherSymbol:
    try:
        return _WMO_SYMBOLS[weather_code]
    except KeyError as error:
        raise ValueError(f"unsupported weather code: {weather_code}") from error
