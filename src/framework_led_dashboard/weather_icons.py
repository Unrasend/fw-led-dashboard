from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .frame import Frame
from .weather_codes import WeatherIntensity, WeatherKind, WeatherSymbol, classify_weather_code

Icon = tuple[str, ...]
Art = tuple[str, ...]
ICON_WIDTH = 9
ICON_HEIGHT = 10
ICON_FRAMES = 20
ICON_INTENSITIES = {"L": 128, "#": 220}
_PIXEL_RANK = {".": 0, "L": 1, "#": 2}
_RAIN_TOP = 5
_RAIN_ROWS = 5

CLOUD: Art = (".####.", "######", ".####.")
NIGHT_CLOUD: Art = (".####.", "######", ".#####")
BIG_CLOUD: Art = ("..##.....", ".####.##.", "#########", ".#######.")
FOG_CLOUD: Art = ("..##.##..", "#########", ".#######.")
SMALL_FOG_CLOUD: Art = (".###.", "#####", ".###.")
WIDE_CLOUD: Art = (".##.##.", "#######", ".#####.")
SMALL_CLOUD: Art = (".##.", "####")
MOON: Art = (".##", "#..", "#..", ".##")
BIG_MOON: Art = ("..##", ".#..", "#...", ".#..", "..##")
SUN_SMALL: Art = ("L#L", "###", "L#L")
BOLT: Art = ("..#", ".#.", "###", ".#.", "#..")
FLAKE: Art = (".#.", "#.#", ".#.")


class Sky(Enum):
    NONE = "none"
    BRIGHT = "bright"
    DIM = "dim"
    FLAKE = "flake"


@dataclass(frozen=True, slots=True)
class Layout:
    cloud: Art
    cloud_x: int
    cloud_y: int
    sky_x: int
    rain_starts: tuple[int, ...]
    snow_columns: tuple[int, ...]
    snow_extra_column: int
    bolt_x: int
    twin_bolts: tuple[int, int]
    hail_light: tuple[int, ...]
    hail_heavy: tuple[int, ...]


DAY_COMPACT = Layout(CLOUD, 3, 2, 0, (8, 6, 4), (4, 6, 8), 6, 6, (3, 6), (3, 5), (3, 4, 5))
DAY_BIG = Layout(BIG_CLOUD, 0, 1, 0, (7, 5, 3), (1, 4, 7), 4, 6, (1, 5), (1, 3), (0, 2, 4))
NIGHT = Layout(NIGHT_CLOUD, 0, 2, 6, (5, 3, 1), (1, 3, 5), 3, 4, (0, 4), (0, 2), (0, 1, 2))


class IconCanvas:
    def __init__(self) -> None:
        self._grid = [["." for _x in range(ICON_WIDTH)] for _y in range(ICON_HEIGHT)]

    def put(self, x: int, y: int, pixel: str) -> None:
        inside = 0 <= x < ICON_WIDTH and 0 <= y < ICON_HEIGHT
        if inside and _PIXEL_RANK[pixel] > _PIXEL_RANK[self._grid[y][x]]:
            self._grid[y][x] = pixel

    def stamp(self, art: Art, x0: int, y0: int, pixel: str | None = None) -> None:
        for dy, row in enumerate(art):
            for dx, char in enumerate(row):
                if char != ".":
                    self.put(x0 + dx, y0 + dy, pixel or char)

    def rows(self) -> Icon:
        return tuple("".join(row) for row in self._grid)


@dataclass(frozen=True, slots=True)
class Scene:
    symbol: WeatherSymbol
    is_day: bool
    frame: int

    @property
    def kind(self) -> WeatherKind:
        return self.symbol.kind

    @property
    def light(self) -> bool:
        return self.symbol.intensity is WeatherIntensity.LIGHT

    @property
    def heavy(self) -> bool:
        return self.symbol.intensity is WeatherIntensity.HEAVY


class WeatherIconArtist:
    def __init__(self, scene: Scene) -> None:
        self._scene = scene
        self._canvas = IconCanvas()

    def draw(self) -> Icon:
        kind = self._scene.kind
        if kind is WeatherKind.CLEAR:
            self._big_sky(center_x=4, center_y=4, cloud=False)
        elif kind is WeatherKind.MAINLY_CLEAR:
            self._big_sky(center_x=3, center_y=3, cloud=True)
        elif kind is WeatherKind.PARTLY_CLOUDY:
            self._partly_cloudy()
        elif kind is WeatherKind.OVERCAST:
            self._overcast()
        elif kind in {WeatherKind.FOG, WeatherKind.RIME_FOG}:
            self._fog(rime=kind is WeatherKind.RIME_FOG)
        elif kind in {WeatherKind.DRIZZLE, WeatherKind.FREEZING_DRIZZLE}:
            self._drizzle(freezing=kind is WeatherKind.FREEZING_DRIZZLE)
        elif kind in {WeatherKind.RAIN, WeatherKind.FREEZING_RAIN, WeatherKind.RAIN_SHOWERS}:
            self._rain(kind)
        elif kind is WeatherKind.SNOW_GRAINS:
            self._snow(self._cloud(Sky.DIM), count=3, speed=2, pixel="L")
        elif kind in {WeatherKind.SNOW, WeatherKind.SNOW_SHOWERS}:
            sky = Sky.BRIGHT if kind is WeatherKind.SNOW_SHOWERS else Sky.DIM
            count = 3 if self._scene.light else 4
            self._snow(self._cloud(sky), count=count, speed=1, pixel="#", dense=self._scene.heavy)
        elif kind is WeatherKind.THUNDER:
            self._thunder()
        elif kind is WeatherKind.THUNDER_HAIL:
            self._thunder_hail()
        return self._canvas.rows()

    def _layout(self, mode: Sky) -> Layout:
        if not self._scene.is_day:
            return NIGHT
        return DAY_BIG if mode is Sky.DIM else DAY_COMPACT

    def _sky(self, mode: Sky, layout: Layout) -> None:
        x = layout.sky_x
        if mode is Sky.FLAKE:
            self._canvas.stamp(FLAKE, x, 0)
            return
        if self._scene.is_day:
            if mode is Sky.BRIGHT:
                self._canvas.stamp(SUN_SMALL, x, 0)
                if self._scene.frame % 2 == 0:
                    self._canvas.put(x + 3, 1, "L")
                    self._canvas.put(x + 1, 3, "L")
            return
        self._canvas.stamp(MOON, x, 0, "#" if mode is Sky.BRIGHT else "L")

    def _cloud(self, mode: Sky) -> Layout:
        layout = self._layout(mode)
        self._sky(mode, layout)
        self._canvas.stamp(layout.cloud, layout.cloud_x, layout.cloud_y)
        return layout

    def _big_sky(self, *, center_x: int, center_y: int, cloud: bool) -> None:
        frame = self._scene.frame
        if cloud:
            self._canvas.stamp(SMALL_CLOUD, 5, 8)
        if self._scene.is_day:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    self._canvas.put(center_x + dx, center_y + dy, "#")
            cardinal = ((0, -1), (0, 1), (-1, 0), (1, 0))
            diagonal = ((-1, -1), (1, -1), (-1, 1), (1, 1))
            for ux, uy in cardinal if frame % 2 == 0 else diagonal:
                for distance in (3, 4):
                    x, y = center_x + ux * distance, center_y + uy * distance
                    if cloud and x >= 4 and y >= 7:
                        continue
                    self._canvas.put(x, y, "L" if distance == 4 else "#")
            return
        self._canvas.stamp(BIG_MOON, center_x, center_y - 2, "#")
        stars = ((0, 0), (7, 0), (0, 6), (7, 5)) if cloud else ((0, 0), (8, 1), (1, 6), (8, 7))
        for index, (x, y) in enumerate(stars):
            if (frame + index) % 4 in (0, 1):
                self._canvas.put(x, y, "L")

    def _partly_cloudy(self) -> None:
        self._cloud(Sky.BRIGHT)
        if self._scene.is_day:
            return
        for index, (x, y) in enumerate(((7, 6), (1, 7))):
            if (self._scene.frame // 2 + index) % 2 == 0:
                self._canvas.put(x, y, "L")

    def _overcast(self) -> None:
        frame = self._scene.frame
        if self._scene.is_day:
            self._canvas.stamp(BIG_CLOUD, 0, 3)
            for k in range(2):
                self._canvas.put((frame + k * 4) % 9, 1, "L")
            return
        self._sky(Sky.DIM, NIGHT)
        self._canvas.stamp(WIDE_CLOUD, 0, 3)
        for k in range(2):
            x = (frame + k * 3) % 5
            if x < 4:
                self._canvas.put(x, 1, "L")

    def _fog(self, *, rime: bool) -> None:
        is_day = self._scene.is_day
        if is_day and not rime:
            self._canvas.stamp(FOG_CLOUD, 0, 0)
        elif is_day:
            self._sky(Sky.FLAKE, DAY_COMPACT)
            self._canvas.stamp(SMALL_FOG_CLOUD, 4, 0)
        else:
            self._sky(Sky.FLAKE if rime else Sky.DIM, NIGHT)
            self._canvas.stamp(SMALL_FOG_CLOUD, 0, 0)
        for row, y in enumerate((3, 5, 7, 9)):
            clipped = y == 3 and (rime or not is_day)
            if clipped and is_day:
                span = range(3, ICON_WIDTH)
            elif clipped:
                span = range(6)
            else:
                span = range(ICON_WIDTH)
            self._fog_line(y, span=span, reverse=row % 2 == 1)

    def _fog_line(self, y: int, *, span: range, reverse: bool) -> None:
        frame = self._scene.frame
        for x in span:
            if (x - frame if reverse else x + frame) % 4 != 3:
                self._canvas.put(x, y, "L")

    def _drizzle(self, *, freezing: bool) -> None:
        layout = self._cloud(Sky.DIM)
        period, on = (3, 2) if self._scene.heavy else (2, 1)
        speed = 1 if self._scene.light else 2
        self._lines(layout, speed=speed, period=period, on=on, pixel="L", bright_middle=freezing)

    def _rain(self, kind: WeatherKind) -> None:
        skies = {WeatherKind.RAIN_SHOWERS: Sky.BRIGHT, WeatherKind.FREEZING_RAIN: Sky.FLAKE}
        layout = self._cloud(skies.get(kind, Sky.DIM))
        speed = 1 if self._scene.light else 2
        on = 3 if self._scene.heavy else 2
        self._lines(layout, speed=speed, period=4, on=on, pixel="#")

    def _lines(
        self,
        layout: Layout,
        *,
        speed: int,
        period: int,
        on: int,
        pixel: str,
        bright_middle: bool = False,
    ) -> None:
        frame = self._scene.frame
        phase = frame // 2 if speed == 1 else frame
        for index, x0 in enumerate(layout.rain_starts):
            line_pixel = "#" if bright_middle and index == 1 else pixel
            for step in range(_RAIN_ROWS):
                if (step - phase) % period < on:
                    self._canvas.put(x0 - step, _RAIN_TOP + step, line_pixel)

    def _snow(self, layout: Layout, *, count: int, speed: int, pixel: str, dense: bool = False) -> None:
        frame = self._scene.frame
        for index, x in enumerate(layout.snow_columns):
            self._canvas.put(x, _RAIN_TOP + (speed * frame + index * 2) % _RAIN_ROWS, pixel)
            if dense or (count == 4 and x == layout.snow_extra_column):
                self._canvas.put(x, _RAIN_TOP + (speed * frame + index * 2 + 3) % _RAIN_ROWS, pixel)

    def _thunder(self) -> None:
        layout = self._cloud(Sky.DIM)
        left, right = layout.twin_bolts
        frame = self._scene.frame
        self._bolt(left, visible=frame % 4 != 1)
        self._bolt(right, visible=frame % 4 != 3)

    def _thunder_hail(self) -> None:
        layout = self._cloud(Sky.DIM)
        frame = self._scene.frame
        self._bolt(layout.bolt_x, visible=frame % 4 != 1)
        columns = layout.hail_heavy if self._scene.heavy else layout.hail_light
        for index, x in enumerate(columns):
            self._canvas.put(x, _RAIN_TOP + (frame + index * 2) % _RAIN_ROWS, "#")
            self._canvas.put(x, _RAIN_TOP + (frame + index * 2 + 3) % _RAIN_ROWS, "#")

    def _bolt(self, x: int, *, visible: bool) -> None:
        if visible:
            self._canvas.stamp(BOLT, x, _RAIN_TOP)


def weather_icon(symbol: WeatherSymbol, *, is_day: bool, frame_index: int) -> Icon:
    return WeatherIconArtist(Scene(symbol, is_day, frame_index % ICON_FRAMES)).draw()


def draw_weather_icon(frame: Frame, weather_code: int, *, is_day: bool, frame_index: int) -> None:
    icon = weather_icon(classify_weather_code(weather_code), is_day=is_day, frame_index=frame_index)
    for y, row in enumerate(icon):
        for x, pixel in enumerate(row):
            if pixel in ICON_INTENSITIES:
                frame.set_pixel(x, y, ICON_INTENSITIES[pixel])
