from __future__ import annotations

from .frame import Frame
from .weather_codes import (
    WeatherIntensity,
    WeatherKind,
    WeatherSymbol,
    classify_weather_code,
)

Icon = tuple[str, str, str, str, str, str, str, str]
Grid = list[list[str]]
ICON_INTENSITIES = {"L": 128, "#": 220}
_PIXEL_RANK = {".": 0, "L": 1, "#": 2}
_CLEAR_DAY_ICONS: tuple[Icon, Icon] = (
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
)


def weather_icon(symbol: WeatherSymbol, *, is_day: bool, frame_index: int) -> Icon:
    frame = frame_index % 2
    if symbol.kind is WeatherKind.CLEAR and is_day:
        return _CLEAR_DAY_ICONS[frame]
    grid = [["." for _x in range(9)] for _y in range(8)]
    kind = symbol.kind

    if kind is WeatherKind.CLEAR:
        _draw_clear(grid, is_day=is_day, frame=frame)
    elif kind is WeatherKind.MAINLY_CLEAR:
        _draw_clear(grid, is_day=is_day, frame=frame)
        _draw_small_cloud(grid, frame=frame)
    elif kind is WeatherKind.PARTLY_CLOUDY:
        _draw_sky_marker(grid, is_day=is_day, frame=frame)
        _draw_cloud(grid, frame=frame)
    elif kind is WeatherKind.OVERCAST:
        _draw_sky_marker(grid, is_day=is_day, frame=frame)
        _draw_cloud(grid, frame=frame)
        _draw_overcast_layer(grid, frame=frame)
    elif kind in {WeatherKind.FOG, WeatherKind.RIME_FOG}:
        _draw_sky_marker(grid, is_day=is_day, frame=frame)
        _draw_cloud(grid, frame=frame)
        _draw_fog(grid, frame=frame, freezing=kind is WeatherKind.RIME_FOG)
    else:
        _draw_sky_marker(grid, is_day=is_day, frame=frame)
        _draw_cloud(grid, frame=frame)
        _draw_condition(grid, symbol=symbol, frame=frame)

    return tuple("".join(row) for row in grid)  # type: ignore[return-value]


def draw_weather_icon(
    frame: Frame,
    weather_code: int,
    *,
    is_day: bool,
    frame_index: int,
) -> None:
    icon = weather_icon(
        classify_weather_code(weather_code),
        is_day=is_day,
        frame_index=frame_index,
    )
    for y, row in enumerate(icon):
        for x, pixel in enumerate(row):
            if pixel in ICON_INTENSITIES:
                frame.set_pixel(x, y, ICON_INTENSITIES[pixel])


def _draw_clear(grid: Grid, *, is_day: bool, frame: int) -> None:
    if is_day:
        for x, y in ((4, 2), (3, 3), (4, 3), (5, 3), (4, 4)):
            _set(grid, x, y, "#")
        rays = (
            ((4, 0), (1, 3), (7, 3), (4, 6)),
            ((2, 1), (6, 1), (2, 5), (6, 5)),
        )[frame]
        for x, y in rays:
            _set(grid, x, y, "L")
        return

    for x, y in ((4, 1), (3, 2), (3, 3), (3, 4), (4, 5), (5, 5), (6, 4)):
        _set(grid, x, y, "#")
    stars = (
        ((1, 1), (7, 2), (1, 6)),
        ((2, 0), (7, 5), (1, 3)),
    )[frame]
    for x, y in stars:
        _set(grid, x, y, "L")


def _draw_sky_marker(grid: Grid, *, is_day: bool, frame: int) -> None:
    if is_day:
        points = ((0, 0, "#"), (1, 0, "L"), (0, 1, "L")) if frame == 0 else (
            (1, 0, "#"),
            (0, 0, "L"),
            (2, 0, "L"),
            (1, 1, "L"),
        )
    else:
        points = ((0, 0, "L"), (1, 0, "#"), (1, 1, "#")) if frame == 0 else (
            (0, 0, "#"),
            (0, 1, "#"),
            (1, 1, "L"),
            (2, 0, "L"),
        )
    for x, y, pixel in points:
        _set(grid, x, y, pixel)


def _draw_small_cloud(grid: Grid, *, frame: int) -> None:
    for x, y in ((6, 3), (7, 3), (5, 4), (8, 4), (5, 5), (6, 5), (7, 5), (8, 5)):
        _set(grid, x, y, "#")
    _set(grid, 5 if frame == 0 else 8, 6, "L")


def _draw_cloud(grid: Grid, *, frame: int) -> None:
    points = (
        (4, 0),
        (5, 0),
        (3, 1),
        (6, 1),
        (1, 2),
        (2, 2),
        (3, 2),
        (7, 2),
        (0, 3),
        (8, 3),
        *((x, 4) for x in range(1, 8)),
    )
    for x, y in points:
        _set(grid, x, y, "#")
    _set(grid, 2 if frame == 0 else 6, 1, "L")


def _draw_overcast_layer(grid: Grid, *, frame: int) -> None:
    for x in range(0 if frame == 0 else 2, 7 if frame == 0 else 9):
        _set(grid, x, 6, "L")
    _set(grid, 8 if frame == 0 else 0, 5, "#")


def _draw_fog(grid: Grid, *, frame: int, freezing: bool) -> None:
    starts = (0, 2) if frame == 0 else (2, 0)
    for x in range(starts[0], starts[0] + 7):
        _set(grid, x, 5, "L")
    for x in range(starts[1], starts[1] + 7):
        _set(grid, x, 7, "L")
    if freezing:
        center = 7 if frame == 0 else 1
        _set(grid, center, 6, "#")
        _set(grid, center - 1, 6, "L")
        _set(grid, center + 1, 6, "L")


def _draw_condition(grid: Grid, *, symbol: WeatherSymbol, frame: int) -> None:
    kind = symbol.kind
    if kind in {WeatherKind.DRIZZLE, WeatherKind.FREEZING_DRIZZLE}:
        _draw_precipitation(
            grid,
            intensity=symbol.intensity,
            frame=frame,
            pixels=("L", "L"),
        )
        if kind is WeatherKind.FREEZING_DRIZZLE:
            _draw_ice_mark(grid, frame=frame)
    elif kind in {WeatherKind.RAIN, WeatherKind.FREEZING_RAIN, WeatherKind.RAIN_SHOWERS}:
        _draw_precipitation(
            grid,
            intensity=symbol.intensity,
            frame=frame,
            pixels=("#", "#"),
        )
        if kind is WeatherKind.FREEZING_RAIN:
            _draw_ice_mark(grid, frame=frame)
        if kind is WeatherKind.RAIN_SHOWERS:
            _set(grid, 8 if frame == 0 else 0, 1, "L")
    elif kind in {WeatherKind.SNOW, WeatherKind.SNOW_SHOWERS}:
        _draw_precipitation(
            grid,
            intensity=symbol.intensity,
            frame=frame,
            pixels=("#", "L"),
        )
        if kind is WeatherKind.SNOW_SHOWERS:
            _set(grid, 8 if frame == 0 else 0, 1, "L")
    elif kind is WeatherKind.SNOW_GRAINS:
        _draw_precipitation(
            grid,
            intensity=WeatherIntensity.MODERATE,
            frame=frame,
            pixels=("L", "L"),
        )
    elif kind in {WeatherKind.THUNDER, WeatherKind.THUNDER_HAIL}:
        _draw_precipitation(
            grid,
            intensity=symbol.intensity,
            frame=frame,
            pixels=("#", "#"),
        )
        _draw_bolt(grid, frame=frame)
        if kind is WeatherKind.THUNDER_HAIL:
            _draw_hail(grid, intensity=symbol.intensity, frame=frame)


def _draw_precipitation(
    grid: Grid,
    *,
    intensity: WeatherIntensity,
    frame: int,
    pixels: tuple[str, str],
) -> None:
    stroke_starts = (
        ((0, 5), (3, 5), (6, 5), (1, 6), (5, 6)),
        ((1, 5), (4, 5), (7, 5), (0, 6), (4, 6)),
    )[frame]
    for x, y in stroke_starts[: _particle_count(intensity)]:
        _set(grid, x, y, pixels[0])
        _set(grid, x + 1, y + 1, pixels[1])


def _draw_ice_mark(grid: Grid, *, frame: int) -> None:
    center = 7 if frame == 0 else 1
    _set(grid, center, 7, "#")
    _set(grid, center - 1, 7, "L")
    _set(grid, center + 1, 7, "L")


def _draw_bolt(grid: Grid, *, frame: int) -> None:
    offset = frame
    for x, y in ((4 + offset, 5), (3 + offset, 6), (4 + offset, 6), (3 + offset, 7)):
        _set(grid, x, y, "#")


def _draw_hail(grid: Grid, *, intensity: WeatherIntensity, frame: int) -> None:
    positions = (((1, 6), (7, 7), (1, 7), (7, 6)), ((2, 7), (8, 6), (0, 6), (6, 7)))[
        frame
    ]
    count = 1 if intensity is WeatherIntensity.LIGHT else 4
    for x, y in positions[:count]:
        _set(grid, x, y, "L")


def _particle_count(intensity: WeatherIntensity) -> int:
    return {
        WeatherIntensity.STANDARD: 3,
        WeatherIntensity.LIGHT: 2,
        WeatherIntensity.MODERATE: 3,
        WeatherIntensity.HEAVY: 5,
    }[intensity]


def _set(grid: Grid, x: int, y: int, pixel: str) -> None:
    if 0 <= x < 9 and 0 <= y < 8 and _PIXEL_RANK[pixel] > _PIXEL_RANK[grid[y][x]]:
        grid[y][x] = pixel
