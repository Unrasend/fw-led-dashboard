from __future__ import annotations

import cmath
import io
import math
import random
import struct
import time
from itertools import pairwise
from typing import Self

import pytest

from framework_led_dashboard.audio import (
    BAND_COUNT,
    SAMPLE_RATE,
    WINDOW_SIZE,
    BandLayout,
    CaptureRequest,
    RealPowerSpectrum,
    SoundSpectrum,
    SpectrumAnalyzer,
    capture_command,
    parec_command,
    pw_record_command,
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


def test_real_power_spectrum_matches_a_plain_dft() -> None:
    rng = random.Random(5)
    samples = [rng.uniform(-1, 1) for _ in range(64)]
    pairs = [complex(samples[n], samples[n + 1]) for n in range(0, 64, 2)]

    powers = RealPowerSpectrum(64, 32).power(pairs)

    expected = [
        abs(sum(x * cmath.exp(-2j * math.pi * k * n / 64) for n, x in enumerate(samples))) ** 2
        for k in range(32)
    ]
    assert powers == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_pcm_input_gives_the_same_levels_as_float_samples() -> None:
    signal = SignalBuilder().tone(440.0, 0.4).noise(0.05).build()
    pcm = struct.pack(f"<{WINDOW_SIZE}h", *(round(sample * 32767) for sample in signal))
    as_floats = [value / 32768 for value in struct.unpack(f"<{WINDOW_SIZE}h", pcm)]
    from_pcm, from_floats = AnalyzerBuilder().build(), AnalyzerBuilder().build()

    for _ in range(5):
        assert from_pcm.analyze_pcm16(pcm) == pytest.approx(from_floats.analyze(as_floats))


def test_analyzer_rejects_a_pcm_chunk_of_the_wrong_size() -> None:
    with pytest.raises(ValueError, match="bytes"):
        AnalyzerBuilder().build().analyze_pcm16(b"\x00" * 100)


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


def test_pw_record_streams_the_same_raw_pcm_to_stdout() -> None:
    command = pw_record_command(device=None, sample_rate=SAMPLE_RATE)

    assert command[0] == "pw-record"
    assert {"--raw", "--format=s16", f"--rate={SAMPLE_RATE}", "--channels=1"} <= set(command)
    assert command[-1] == "-"
    assert pw_record_command(device="mic", sample_rate=SAMPLE_RATE)[-2:] == ("--target=mic", "-")
    assert "-P" not in command


def test_pw_record_captures_what_the_default_output_plays_when_asked() -> None:
    command = pw_record_command(device=None, sample_rate=SAMPLE_RATE, channels=2, capture_sink=True)

    assert "--channels=2" in command
    assert command[command.index("-P") + 1] == "{ stream.capture.sink = true }"
    assert command[-1] == "-"  # no target: the stream follows the default output


@pytest.mark.parametrize(
    ("installed", "tool"),
    [({"parec", "pw-record"}, "parec"), ({"pw-record"}, "pw-record"), ({"parec"}, "parec")],
)
def test_capture_prefers_parec_and_falls_back_to_pw_record(installed: set[str], tool: str) -> None:
    command = capture_command(
        sample_rate=SAMPLE_RATE,
        which=lambda name: f"/usr/bin/{name}" if name in installed else None,
    )

    assert command[0] == tool
    assert "--channels=1" in command


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        ({"parec", "pw-record"}, ("pw-record", "-P")),
        ({"pw-record"}, ("pw-record", "-P")),
        ({"parec"}, ("parec", "--device=@DEFAULT_MONITOR@")),
    ],
)
def test_system_sound_prefers_pw_record_and_falls_back_to_the_default_monitor(
    installed: set[str], expected: tuple[str, str]
) -> None:
    command = capture_command(
        CaptureRequest("system", 2),
        sample_rate=SAMPLE_RATE,
        which=lambda name: f"/usr/bin/{name}" if name in installed else None,
    )

    assert command[0] == expected[0] and expected[1] in command
    assert "--channels=2" in command


@pytest.mark.parametrize("source", ["microphone", "system"])
def test_capture_without_either_tool_fails_like_a_missing_program(source: str) -> None:
    with pytest.raises(FileNotFoundError, match="parec nor pw-record"):
        capture_command(CaptureRequest(source), sample_rate=SAMPLE_RATE, which=lambda _name: None)


@pytest.mark.parametrize(("source", "channels"), [("line-in", 1), ("system", 3), ("system", 0)])
def test_capture_request_rejects_unknown_sources_and_channel_counts(source: str, channels: int) -> None:
    with pytest.raises(ValueError):
        CaptureRequest(source, channels)


def test_parec_command_requests_mono_sixteen_bit_pcm_at_analyzer_rate() -> None:
    command = parec_command(device=None, sample_rate=SAMPLE_RATE)

    assert command[0] == "parec"
    assert "--raw" in command and "--format=s16le" in command and "--channels=1" in command
    assert f"--rate={SAMPLE_RATE}" in command
    assert parec_command(device="mic", sample_rate=SAMPLE_RATE)[-1] == "--device=mic"


def _pcm(samples: list[float]) -> bytes:
    return b"".join(int(sample * 32767).to_bytes(2, "little", signed=True) for sample in samples)


def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.01)


def test_sound_spectrum_publishes_levels_from_pcm_stream_restarts_and_goes_stale() -> None:
    payload = _pcm(SignalBuilder().tone(1000.0, 0.3).build()) * 20
    processes: list[FakeProcess] = []
    requests: list[CaptureRequest] = []
    restart_delays: list[float] = []
    now = [0.0]

    def factory(request: CaptureRequest) -> FakeProcess:
        requests.append(request)
        processes.append(FakeProcess(payload if not processes else b""))
        return processes[-1]

    sound = SoundSpectrum(
        analyzer_factory=AnalyzerBuilder().attack(1.0).build,
        process_factory=factory,
        clock=lambda: now[0],
        sleeper=restart_delays.append,
        stale_after_seconds=1.0,
        restart_initial_seconds=0.5,
        restart_max_seconds=2.0,
    )
    sound.start()
    try:
        _wait_for(lambda: len(restart_delays) >= 3)
        spectrum = sound.levels()
        running = sound.request
    finally:
        sound.stop()

    assert spectrum is not None and spectrum.left[5] == pytest.approx(1.0)
    assert spectrum.right is spectrum.left  # one channel serves both modules
    assert running == CaptureRequest() and set(requests) == {CaptureRequest()}
    assert restart_delays[:3] == [0.5, 1.0, 2.0]
    assert all(process.poll() == 0 for process in processes[:3])
    assert sound.request is None and sound.levels() is None


def test_sound_spectrum_analyses_left_and_right_apart() -> None:
    left = SignalBuilder().tone(1000.0, 0.3).build()
    right = SignalBuilder().tone(150.0, 0.3).build()
    interleaved = [sample for pair in zip(left, right, strict=True) for sample in pair]
    payload = _pcm(interleaved) * 20
    sound = SoundSpectrum(
        analyzer_factory=AnalyzerBuilder().attack(1.0).build,
        process_factory=lambda request: FakeProcess(payload),
        sleeper=lambda _delay: time.sleep(0.01),
    )
    sound.start(CaptureRequest("system", 2))
    try:
        _wait_for(lambda: sound.levels() is not None)
        spectrum = sound.levels()
    finally:
        sound.stop()

    assert spectrum is not None
    assert spectrum.left[5] == pytest.approx(1.0) and spectrum.left[1] < 0.1
    assert spectrum.right[1] > 0.9 and spectrum.right[5] < 0.1
    assert spectrum.side("left") is spectrum.left and spectrum.side("right") is spectrum.right


def test_sound_spectrum_restarts_only_for_a_different_request() -> None:
    started: list[CaptureRequest] = []

    def factory(request: CaptureRequest) -> FakeProcess:
        started.append(request)
        return FakeProcess(b"")  # ends at once; the restart loop waits in the sleeper

    sound = SoundSpectrum(process_factory=factory, sleeper=lambda _delay: time.sleep(0.05))
    sound.start(CaptureRequest("microphone"))
    _wait_for(lambda: len(started) >= 1)
    sound.start(CaptureRequest("microphone"))  # the same: keeps running
    assert sound.request == CaptureRequest("microphone")
    sound.start(CaptureRequest("system", 2))
    _wait_for(lambda: CaptureRequest("system", 2) in started)
    sound.stop()

    assert sound.request is None
    assert started[0] == CaptureRequest("microphone")
    assert started[-1] == CaptureRequest("system", 2)
