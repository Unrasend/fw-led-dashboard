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
# The dim level sits far below the bright one: the panel's PWM is linear, and the eye is not.
ICON_INTENSITIES = {"L": 64, "#": 220}
_PIXEL_RANK = {".": 0, "L": 1, "#": 2}
_RAIN_TOP = 5
_RAIN_ROWS = 5

CLOUD: Art = (".####.", "######", ".####.")
# Its hump leans away from the moon, so the two never read as one shape.
NIGHT_CLOUD: Art = (".##...", "#####.", "######")
BIG_CLOUD: Art = ("..##.....", ".####.##.", "#########", ".#######.")
FOG_CLOUD: Art = ("..##.##..", "#########", ".#######.")
SMALL_FOG_CLOUD: Art = (".###.", "#####", ".###.")
WIDE_CLOUD: Art = (".##.##.", "#######", ".#####.")
SMALL_CLOUD: Art = (".##.", "####")
MOON: Art = (".##", "##.", "##.", ".##")
BIG_MOON: Art = (".###", "###.", "##..", "##..", "###.", ".###")
SUN_SMALL: Art = ("L#L", "###", "L#L")
BOLT: Art = ("..##", ".##.", "####", ".##.", ".#..")
FLAKE: Art = ("#.#", ".#.", "#.#")
FLAKE_FILLED: Art = ("#L#", "L#L", "#L#")
BIG_FLAKE: Art = ("..#..", "#.#.#", ".###.", "#.#.#", "..#..")
BIG_FLAKE_DIM_TIPS: Art = ("..L..", "L.#.L", ".###.", "L.#.L", "..L..")

# Freezing drizzle, freezing rain and rime fog carry a snowflake in the bottom right corner;
# their precipitation keeps to the two lines on its left.
_FLAKE_X = 6
_FLAKE_Y = 7
_FREEZING_STARTS = (5, 3)
_CLEAR_STARS = ((1, 0), (8, 1), (0, 7), (8, 8))
_MAINLY_CLEAR_STARS = ((0, 0), (8, 1), (7, 5), (0, 8))
_PARTLY_CLOUDY_STARS = ((1, 1), (4, 0))
_PARTLY_CLOUDY_DROP = 2  # nothing falls from this cloud, so it sits in the middle of the icon
_DRIFT_FRAMES = 5  # the far cloud of an overcast sky moves one pixel every 2.5 s


class Sky(Enum):
    BRIGHT = "bright"
    DIM = "dim"


@dataclass(frozen=True, slots=True)
class Layout:
    cloud: Art
    cloud_x: int
    cloud_y: int
    sky_x: int
    rain_starts: tuple[int, ...]
    snowflake_x: int
    snow_columns: tuple[int, int]  # single flakes fall beside the big one; light snow has one
    grain_columns: tuple[int, ...] = ()
    bolts: tuple[int, ...] = ()
    hail_bolt_x: int = 0
    hail_columns: tuple[int, ...] = ()


# The sun shares the icon with a small cloud: no snow grains or thunderstorms in this layout.
DAY_COMPACT = Layout(CLOUD, 3, 2, 0, (8, 6, 4), 4, (2, 0))
DAY_BIG = Layout(
    BIG_CLOUD,
    0,
    1,
    0,
    (8, 6, 4),
    2,
    (0, 8),
    grain_columns=(1, 4, 7),
    bolts=(0, 5),
    hail_bolt_x=5,
    hail_columns=(1, 3),
)
NIGHT = Layout(
    NIGHT_CLOUD,
    0,
    2,
    6,
    (6, 4, 2),
    0,
    (6, 8),
    grain_columns=(1, 3, 5),
    bolts=(1,),
    hail_bolt_x=0,
    hail_columns=(5, 7),
)


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
    """Every animation repeats within ICON_FRAMES, so the loop has no visible seam."""

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
            self._snow_grains(self._cloud(Sky.DIM))
        elif kind in {WeatherKind.SNOW, WeatherKind.SNOW_SHOWERS}:
            self._snow(self._cloud(Sky.BRIGHT if kind is WeatherKind.SNOW_SHOWERS else Sky.DIM))
        elif kind is WeatherKind.THUNDER:
            self._thunder()
        elif kind is WeatherKind.THUNDER_HAIL:
            self._thunder_hail()
        return self._canvas.rows()

    @property
    def _slow_beat(self) -> bool:
        """Alternates once a second: sun rays that flip every frame flicker at the eye's edge."""
        return (self._scene.frame // 2) % 2 == 0

    def _layout(self, mode: Sky) -> Layout:
        if not self._scene.is_day:
            return NIGHT
        return DAY_BIG if mode is Sky.DIM else DAY_COMPACT

    def _sky(self, mode: Sky, layout: Layout, *, drop: int = 0) -> None:
        x = layout.sky_x
        if not self._scene.is_day:
            self._canvas.stamp(MOON, x, drop, "#" if mode is Sky.BRIGHT else "L")
        elif mode is Sky.BRIGHT:
            self._canvas.stamp(SUN_SMALL, x, drop)
            if self._slow_beat:
                self._canvas.put(x + 3, drop + 1, "L")
                self._canvas.put(x + 1, drop + 3, "L")

    def _cloud(self, mode: Sky, *, drop: int = 0) -> Layout:
        layout = self._layout(mode)
        self._sky(mode, layout, drop=drop)
        self._canvas.stamp(layout.cloud, layout.cloud_x, layout.cloud_y + drop)
        return layout

    def _big_sky(self, *, center_x: int, center_y: int, cloud: bool) -> None:
        if cloud:
            self._canvas.stamp(SMALL_CLOUD, 5, 8)
        if self._scene.is_day:
            self._big_sun(center_x, center_y, long_rays=not cloud)
            return
        self._canvas.stamp(BIG_MOON, center_x - 1, center_y - 2)
        self._stars(_MAINLY_CLEAR_STARS if cloud else _CLEAR_STARS)

    def _big_sun(self, center_x: int, center_y: int, *, long_rays: bool) -> None:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                self._canvas.put(center_x + dx, center_y + dy, "#")
        cardinal = ((0, -1), (0, 1), (-1, 0), (1, 0))
        diagonal = ((-1, -1), (1, -1), (-1, 1), (1, 1))
        for ux, uy in cardinal if self._slow_beat else diagonal:
            self._canvas.put(center_x + ux * 3, center_y + uy * 3, "#")
            if long_rays:
                self._canvas.put(center_x + ux * 4, center_y + uy * 4, "L")

    def _stars(self, stars: tuple[tuple[int, int], ...]) -> None:
        for index, (x, y) in enumerate(stars):
            if (self._scene.frame + index) % 4 in (0, 1):
                self._canvas.put(x, y, "L")

    def _partly_cloudy(self) -> None:
        self._cloud(Sky.BRIGHT, drop=_PARTLY_CLOUDY_DROP)
        if not self._scene.is_day:
            self._stars(_PARTLY_CLOUDY_STARS)

    def _overcast(self) -> None:
        """A bright cloud in front of a dim one that drifts back and forth."""
        step = self._scene.frame // _DRIFT_FRAMES
        if self._scene.is_day:
            self._canvas.stamp(WIDE_CLOUD, (0, 1, 2, 1)[step % 4], 1, "L")
            self._canvas.stamp(BIG_CLOUD, 0, 4)
            return
        self._sky(Sky.DIM, NIGHT)
        self._canvas.stamp(SMALL_FOG_CLOUD, step % 2, 3, "L")
        self._canvas.stamp(BIG_CLOUD, 0, 6)

    def _fog(self, *, rime: bool) -> None:
        is_day = self._scene.is_day
        if is_day:
            self._canvas.stamp(FOG_CLOUD, 0, 0)
        else:
            self._sky(Sky.DIM, NIGHT)
            self._canvas.stamp(SMALL_FOG_CLOUD, 0, 0)
        for row, y in enumerate((3, 5, 7, 9)):
            width = ICON_WIDTH
            if y == 3 and not is_day:
                width = NIGHT.sky_x - 1  # the moon hangs over the end of the first line
            if rime and y >= _FLAKE_Y:
                width = _FLAKE_X - 1
            self._fog_line(y, span=range(width), reverse=row % 2 == 1)
        if rime:
            self._flake()

    def _fog_line(self, y: int, *, span: range, reverse: bool) -> None:
        frame = self._scene.frame
        for x in span:
            if (x - frame if reverse else x + frame) % 4 != 3:
                self._canvas.put(x, y, "L")

    def _flake(self) -> None:
        """The crystal glints: its gaps fill with dim light every other second."""
        self._canvas.stamp(FLAKE if self._slow_beat else FLAKE_FILLED, _FLAKE_X, _FLAKE_Y)

    def _drizzle(self, *, freezing: bool) -> None:
        """Dim drops: single ones that fall slowly or fast, short strokes when it is dense."""
        layout = self._cloud(Sky.DIM)
        starts = _FREEZING_STARTS if freezing else layout.rain_starts
        if self._scene.light:
            self._lines(starts, period=5, on=1, pixel="L", slow=True)
        else:
            self._lines(starts, period=4, on=2 if self._scene.heavy else 1, pixel="L")
        if freezing:
            self._flake()

    def _rain(self, kind: WeatherKind) -> None:
        layout = self._cloud(Sky.BRIGHT if kind is WeatherKind.RAIN_SHOWERS else Sky.DIM)
        freezing = kind is WeatherKind.FREEZING_RAIN
        starts = _FREEZING_STARTS if freezing else layout.rain_starts
        if self._scene.light:
            self._lines(starts, period=5, on=2, pixel="#", slow=True)
        else:
            self._lines(starts, period=4, on=3 if self._scene.heavy else 2, pixel="#")
        if freezing:
            self._flake()

    def _lines(
        self,
        starts: tuple[int, ...],
        *,
        period: int,
        on: int,
        pixel: str,
        slow: bool = False,
    ) -> None:
        """Drops slide down and to the left, each line two steps behind the one on its right.

        A slow line moves every other frame, so `period` must divide half of ICON_FRAMES."""
        frame = self._scene.frame
        phase = frame // 2 if slow else frame
        for index, x0 in enumerate(starts):
            for step in range(_RAIN_ROWS):
                if (step - phase - index * 2) % period < on:
                    self._canvas.put(x0 - step, _RAIN_TOP + step, pixel)

    def _snow(self, layout: Layout) -> None:
        """One big snowflake under the cloud, its tips twinkling; single flakes fall beside it:
        one slowly when the snow is light, two for snow, four at twice the speed when heavy."""
        scene = self._scene
        art = BIG_FLAKE if self._slow_beat else BIG_FLAKE_DIM_TIPS
        self._canvas.stamp(art, layout.snowflake_x, _RAIN_TOP)
        columns = layout.snow_columns[:1] if scene.light else layout.snow_columns
        step = scene.frame if scene.heavy else scene.frame // 2
        for index, x in enumerate(columns):
            self._canvas.put(x, _RAIN_TOP + (step + index * 2) % _RAIN_ROWS, "#")
            if scene.heavy:
                self._canvas.put(x, _RAIN_TOP + (step + index * 2 + 3) % _RAIN_ROWS, "#")

    def _snow_grains(self, layout: Layout) -> None:
        """Too small for a snowflake: dim grains that fall fast."""
        for index, x in enumerate(layout.grain_columns):
            y = _RAIN_TOP + (self._scene.frame * 2 + index * 2) % _RAIN_ROWS
            self._canvas.put(x, y, "L")

    def _thunder(self) -> None:
        layout = self._cloud(Sky.DIM)
        for index, x in enumerate(layout.bolts):
            self._bolt(x, visible=self._scene.frame % 4 != 1 + index * 2)

    def _thunder_hail(self) -> None:
        """Hail falls twice as fast as snow and drags a trail, longer when it is heavy."""
        layout = self._cloud(Sky.DIM)
        frame = self._scene.frame
        self._bolt(layout.hail_bolt_x, visible=frame % 4 != 1)
        trail = ("#", "L") if self._scene.heavy else ("L",)
        for index, x in enumerate(layout.hail_columns):
            y = _RAIN_TOP + (frame * 2 + index * 2) % _RAIN_ROWS
            self._canvas.put(x, y, "#")
            for distance, pixel in enumerate(trail, start=1):
                if y - distance >= _RAIN_TOP:
                    self._canvas.put(x, y - distance, pixel)

    def _bolt(self, x: int, *, visible: bool) -> None:
        if visible:
            self._canvas.stamp(BOLT, x, _RAIN_TOP)


def weather_icon(symbol: WeatherSymbol, *, is_day: bool, frame_index: int) -> Icon:
    return WeatherIconArtist(Scene(symbol, is_day, frame_index % ICON_FRAMES)).draw()


def draw_weather_icon(
    frame: Frame, weather_code: int, *, is_day: bool, frame_index: int, top: int = 0
) -> None:
    icon = weather_icon(classify_weather_code(weather_code), is_day=is_day, frame_index=frame_index)
    for y, row in enumerate(icon):
        for x, pixel in enumerate(row):
            if pixel in ICON_INTENSITIES:
                frame.set_pixel(x, top + y, ICON_INTENSITIES[pixel])
