from __future__ import annotations

import io
import math
import random
import time
from itertools import pairwise
from typing import Self

import pytest

from framework_led_dashboard.audio import (
    BAND_COUNT,
    SAMPLE_RATE,
    WINDOW_SIZE,
    BandLayout,
    MicrophoneSpectrum,
    SpectrumAnalyzer,
    parec_command,
)
from framework_led_dashboard.equalizer import draw_equalizer, equalizer_bar_heights
from framework_led_dashboard.frame import Frame


class SignalBuilder:
    def __init__(self) -> None:
        self._tones: list[tuple[float, float]] = []
        self._noise = 0.0
        self._seed = 1
        self._samples = WINDOW_SIZE

    def tone(self, frequency_hz: float, amplitude: float) -> Self:
        self._tones.append((frequency_hz, amplitude))
        return self

    def noise(self, amplitude: float) -> Self:
        self._noise = amplitude
        return self

    def samples(self, count: int) -> Self:
        self._samples = count
        return self

    def build(self) -> list[float]:
        generator = random.Random(self._seed)
        return [
            sum(amplitude * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE) for frequency, amplitude in self._tones)
            + generator.gauss(0.0, self._noise)
            for index in range(self._samples)
        ]


class AnalyzerBuilder:
    def __init__(self) -> None:
        self._kwargs: dict[str, float] = {}

    def attack(self, value: float) -> Self:
        self._kwargs["attack"] = value
        return self

    def release(self, value: float) -> Self:
        self._kwargs["release"] = value
        return self

    def build(self) -> SpectrumAnalyzer:
        return SpectrumAnalyzer(**self._kwargs)


class FakeProcess:
    def __init__(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(payload)
        self.terminated = False
        self._exit: int | None = None

    def poll(self) -> int | None:
        return self._exit

    def terminate(self) -> None:
        self.terminated = True
        self._exit = 0

    def wait(self, timeout: float | None = None) -> int:
        self._exit = 0
        return 0


def _settle(analyzer: SpectrumAnalyzer, signal: list[float], rounds: int = 12) -> tuple[float, ...]:
    levels = analyzer.analyze(signal)
    for _ in range(rounds - 1):
        levels = analyzer.analyze(signal)
    return levels


def test_band_layout_covers_nine_log_spaced_bins_below_nyquist() -> None:
    ranges = BandLayout(SAMPLE_RATE, WINDOW_SIZE, 60.0, 6000.0, BAND_COUNT).bin_ranges()

    assert len(ranges) == BAND_COUNT
    assert ranges[0][0] >= 1
    assert all(stop > start for start, stop in ranges)
    assert all(previous[1] == current[0] for previous, current in pairwise(ranges))
    assert ranges[-1][1] <= WINDOW_SIZE // 2


def test_silence_yields_all_zero_levels() -> None:
    analyzer = AnalyzerBuilder().build()

    assert _settle(analyzer, SignalBuilder().build()) == (0.0,) * BAND_COUNT


def test_single_tone_lights_only_its_own_band() -> None:
    analyzer = AnalyzerBuilder().attack(1.0).build()
    _settle(analyzer, SignalBuilder().build())

    levels = _settle(analyzer, SignalBuilder().tone(1000.0, 0.3).build())

    assert levels[5] == pytest.approx(1.0)
    assert all(level == 0.0 for index, level in enumerate(levels) if index != 5)


def test_steady_noise_floor_is_adapted_away_but_a_tone_above_it_shows() -> None:
    analyzer = AnalyzerBuilder().attack(1.0).release(1.0).build()
    noisy = SignalBuilder().noise(0.002).build()
    for _ in range(60):
        analyzer.analyze(noisy)

    assert all(level == 0.0 for level in analyzer.analyze(noisy))
    assert _settle(analyzer, SignalBuilder().noise(0.002).tone(500.0, 0.2).build())[4] > 0.9


def test_levels_decay_after_sound_stops() -> None:
    analyzer = AnalyzerBuilder().attack(1.0).release(0.5).build()
    _settle(analyzer, SignalBuilder().build())
    _settle(analyzer, SignalBuilder().tone(1000.0, 0.3).build())

    first = analyzer.analyze(SignalBuilder().build())[5]
    second = analyzer.analyze(SignalBuilder().build())[5]

    assert first == pytest.approx(0.5)
    assert second == pytest.approx(0.25)


def test_analyzer_rejects_wrong_window_size() -> None:
    with pytest.raises(ValueError, match="expected 512 samples"):
        AnalyzerBuilder().build().analyze(SignalBuilder().samples(100).build())


@pytest.mark.parametrize(("levels", "heights"), [((0.0,) * 9, (0,) * 9), ((1.0,) * 9, (11,) * 9), ((0.5, 2.0, -1.0) + (0.0,) * 6, (6, 11, 0, 0, 0, 0, 0, 0, 0))])
def test_equalizer_bar_heights_clamp_and_round(levels: tuple[float, ...], heights: tuple[int, ...]) -> None:
    assert equalizer_bar_heights(levels) == heights


def test_equalizer_rejects_wrong_band_count() -> None:
    with pytest.raises(ValueError, match="9 band levels"):
        equalizer_bar_heights((0.5,) * 4)


def test_equalizer_draws_dim_body_with_bright_peak_from_the_bottom() -> None:
    frame = Frame()

    draw_equalizer(frame, (3 / 11,) + (0.0,) * 8)

    column = tuple(frame.snapshot()[y][0] for y in range(23, 34))
    assert column == (0,) * 8 + (220, 128, 128)
    assert all(frame.snapshot()[y][x] == 0 for y in range(34) for x in range(1, 9))


def test_equalizer_mirroring_swaps_outer_columns() -> None:
    plain, mirrored = Frame(), Frame()
    levels = (1.0,) + (0.0,) * 8

    draw_equalizer(plain, levels)
    draw_equalizer(mirrored, levels, mirrored=True)

    assert plain.snapshot()[33][0] == 128 and plain.snapshot()[33][8] == 0
    assert mirrored.snapshot()[33][8] == 128 and mirrored.snapshot()[33][0] == 0


def test_parec_command_requests_mono_sixteen_bit_pcm_at_analyzer_rate() -> None:
    command = parec_command(device=None, sample_rate=SAMPLE_RATE)

    assert command[0] == "parec"
    assert "--raw" in command and "--format=s16le" in command and "--channels=1" in command
    assert f"--rate={SAMPLE_RATE}" in command
    assert parec_command(device="mic", sample_rate=SAMPLE_RATE)[-1] == "--device=mic"


def test_microphone_spectrum_publishes_levels_from_pcm_stream_restarts_and_goes_stale() -> None:
    tone = SignalBuilder().tone(1000.0, 0.3).build()
    payload = b"".join(int(sample * 32767).to_bytes(2, "little", signed=True) for sample in tone) * 20
    processes: list[FakeProcess] = []
    restart_delays: list[float] = []
    now = [0.0]

    def factory() -> FakeProcess:
        processes.append(FakeProcess(payload if not processes else b""))
        return processes[-1]

    microphone = MicrophoneSpectrum(
        analyzer=AnalyzerBuilder().attack(1.0).build(),
        process_factory=factory,
        clock=lambda: now[0],
        sleeper=restart_delays.append,
        stale_after_seconds=1.0,
        restart_initial_seconds=0.5,
        restart_max_seconds=2.0,
    )
    microphone.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(restart_delays) < 3:
            time.sleep(0.01)
        levels = microphone.levels()
    finally:
        microphone.stop()

    assert levels is not None and levels[5] == pytest.approx(1.0)
    assert restart_delays[:3] == [0.5, 1.0, 2.0]
    assert all(process.poll() == 0 for process in processes[:3])
    now[0] = 5.0
    assert microphone.levels() is None
