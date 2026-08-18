from __future__ import annotations

from enum import Enum

from .frame import Frame

Icon = tuple[str, str, str, str, str, str, str, str]
ICON_INTENSITIES = {"L": 128, "#": 220}


class WeatherKind(Enum):
    CLEAR = "clear"
    CLOUD = "cloud"
    RAIN = "rain"
    SNOW = "snow"
    THUNDER = "thunder"


_ICONS: dict[WeatherKind, tuple[Icon, Icon]] = {
    WeatherKind.CLEAR: (
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
    WeatherKind.CLOUD: (
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
    WeatherKind.RAIN: (
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
    WeatherKind.SNOW: (
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
    WeatherKind.THUNDER: (
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


def classify_weather_code(weather_code: int) -> WeatherKind:
    if weather_code == 0:
        return WeatherKind.CLEAR
    if weather_code in {71, 73, 75, 77, 85, 86}:
        return WeatherKind.SNOW
    if weather_code in {95, 96, 99}:
        return WeatherKind.THUNDER
    if weather_code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
        return WeatherKind.RAIN
    return WeatherKind.CLOUD


def weather_icon(kind: WeatherKind, *, frame_index: int) -> Icon:
    return _ICONS[kind][frame_index % 2]


def draw_weather_icon(frame: Frame, weather_code: int, *, frame_index: int) -> None:
    icon = weather_icon(classify_weather_code(weather_code), frame_index=frame_index)
    for y, row in enumerate(icon):
        for x, pixel in enumerate(row):
            if pixel in ICON_INTENSITIES:
                frame.set_pixel(x, y, ICON_INTENSITIES[pixel])
