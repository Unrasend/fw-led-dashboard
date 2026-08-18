from __future__ import annotations

from enum import Enum

from .frame import Frame
from .weather_codes import WeatherKind, classify_weather_code

Icon = tuple[str, str, str, str, str, str, str, str]
ICON_INTENSITIES = {"L": 128, "#": 220}


class _IconGroup(Enum):
    CLEAR = "clear"
    CLOUD = "cloud"
    RAIN = "rain"
    SNOW = "snow"
    THUNDER = "thunder"


_ICONS: dict[_IconGroup, tuple[Icon, Icon]] = {
    _IconGroup.CLEAR: (
        (
            "....L....",
            ".L..L..L.",
            "...###...",
            "..#...#..",
            "L.#...#.L",
            "..#...#..",
            "...###...",
            ".L..L..L.",
        ),
        (
            ".L.....L.",
            "....L....",
            "...###...",
            "L.#...#.L",
            "..#...#..",
            "L.#...#.L",
            "...###...",
            "....L....",
        ),
    ),
    _IconGroup.CLOUD: (
        (
            "....##...",
            "...#..#..",
            ".###...#.",
            "#.......#",
            ".#######.",
            ".........",
            ".LL..LL..",
            "LL..LL...",
        ),
        (
            "....##...",
            "...#..#..",
            ".###...#.",
            "#.......#",
            ".#######.",
            ".........",
            "..LL..LL.",
            ".LL..LL..",
        ),
    ),
    _IconGroup.RAIN: (
        (
            "....##...",
            "...#..#..",
            ".###...#.",
            "#.......#",
            ".#######.",
            "..L...L..",
            ".#...#...",
            "...#...#.",
        ),
        (
            "....##...",
            "...#..#..",
            ".###...#.",
            "#.......#",
            ".#######.",
            "...L...L.",
            "#...#....",
            "..#...#..",
        ),
    ),
    _IconGroup.SNOW: (
        (
            "....##...",
            "...#..#..",
            ".###...#.",
            "#.......#",
            ".#######.",
            "..L...L..",
            ".#.#.#...",
            "..L...L..",
        ),
        (
            "....##...",
            "...#..#..",
            ".###...#.",
            "#.......#",
            ".#######.",
            "...L...L.",
            "#.#.#....",
            "...L...L.",
        ),
    ),
    _IconGroup.THUNDER: (
        (
            "....##...",
            "...#..#..",
            ".###...#.",
            "#.......#",
            ".#######.",
            "....##...",
            "...##....",
            "....#....",
        ),
        (
            "....##...",
            "...#..#..",
            ".###...#.",
            "#.......#",
            ".#######.",
            "...##....",
            "....##...",
            "...#.....",
        ),
    ),
}


_ICON_GROUPS = {
    WeatherKind.CLEAR: _IconGroup.CLEAR,
    WeatherKind.MAINLY_CLEAR: _IconGroup.CLEAR,
    WeatherKind.PARTLY_CLOUDY: _IconGroup.CLOUD,
    WeatherKind.OVERCAST: _IconGroup.CLOUD,
    WeatherKind.FOG: _IconGroup.CLOUD,
    WeatherKind.RIME_FOG: _IconGroup.CLOUD,
    WeatherKind.DRIZZLE: _IconGroup.RAIN,
    WeatherKind.FREEZING_DRIZZLE: _IconGroup.RAIN,
    WeatherKind.RAIN: _IconGroup.RAIN,
    WeatherKind.FREEZING_RAIN: _IconGroup.RAIN,
    WeatherKind.RAIN_SHOWERS: _IconGroup.RAIN,
    WeatherKind.SNOW: _IconGroup.SNOW,
    WeatherKind.SNOW_GRAINS: _IconGroup.SNOW,
    WeatherKind.SNOW_SHOWERS: _IconGroup.SNOW,
    WeatherKind.THUNDER: _IconGroup.THUNDER,
    WeatherKind.THUNDER_HAIL: _IconGroup.THUNDER,
}


def weather_icon(kind: WeatherKind, *, frame_index: int) -> Icon:
    return _ICONS[_ICON_GROUPS[kind]][frame_index % 2]


def draw_weather_icon(frame: Frame, weather_code: int, *, frame_index: int) -> None:
    symbol = classify_weather_code(weather_code)
    icon = weather_icon(symbol.kind, frame_index=frame_index)
    for y, row in enumerate(icon):
        for x, pixel in enumerate(row):
            if pixel in ICON_INTENSITIES:
                frame.set_pixel(x, y, ICON_INTENSITIES[pixel])
