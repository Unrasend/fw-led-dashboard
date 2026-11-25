from __future__ import annotations

import cmath
import math
import shutil
import subprocess
import sys
import threading
import time
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import IO, Protocol

BAND_COUNT = 9
SAMPLE_RATE = 16_000
WINDOW_SIZE = 512
Levels = tuple[float, ...]


class SpectrumSource(Protocol):
    def levels(self) -> Levels | None: ...


@dataclass(frozen=True, slots=True)
class BandLayout:
    sample_rate: int
    window_size: int
    low_hz: float
    high_hz: float
    band_count: int

    def bin_ranges(self) -> tuple[tuple[int, int], ...]:
        ratio = (self.high_hz / self.low_hz) ** (1 / self.band_count)
        edges = [self.low_hz * ratio**index for index in range(self.band_count + 1)]
        bin_width = self.sample_rate / self.window_size
        ranges: list[tuple[int, int]] = []
        for low, high in pairwise(edges):
            start = max(1, int(low / bin_width))
            stop = max(start + 1, int(high / bin_width))
            ranges.append((start, min(stop, self.window_size // 2)))
        return tuple(ranges)


def hann_weights(size: int) -> list[float]:
    return [0.5 - 0.5 * math.cos(2 * math.pi * n / size) for n in range(size)]


class RadixTwoFft:
    """In-place iterative complex FFT with per-stage twiddle tables."""

    def __init__(self, size: int) -> None:
        if size < 2 or size & (size - 1):
            raise ValueError("fft size must be a power of two")
        self._size = size
        bits = size.bit_length() - 1
        self._order = [int(f"{index:0{bits}b}"[::-1], 2) for index in range(size)]
        self._stages: list[tuple[int, list[complex]]] = []
        span = 4  # the first stage (span 2) only adds and subtracts
        while span <= size:
            half = span // 2
            twiddles = [cmath.exp(-2j * math.pi * offset / span) for offset in range(half)]
            self._stages.append((span, twiddles))
            span *= 2

    def transform(self, values: Sequence[complex]) -> list[complex]:
        data = [values[index] for index in self._order]
        for start in range(0, self._size, 2):
            first, second = data[start], data[start + 1]
            data[start], data[start + 1] = first + second, first - second
        for span, twiddles in self._stages:
            half = span // 2
            for start in range(0, self._size, span):
                for offset, twiddle in enumerate(twiddles, start):
                    product = twiddle * data[offset + half]
                    data[offset + half] = data[offset] - product
                    data[offset] += product
        return data


class RealPowerSpectrum:
    """|X[k]|² for k < `bins` of `size` real samples.

    The samples go in as size/2 complex pairs (x[2n] + i·x[2n+1]), so one FFT of half the
    length does the work; only the bins the bands use are unpacked.
    """

    def __init__(self, size: int, bins: int) -> None:
        if not 0 < bins <= size // 2:
            raise ValueError(f"bins must be between 1 and {size // 2}")
        self._fft = RadixTwoFft(size // 2)
        self._rotations = [-0.5j * cmath.exp(-2j * math.pi * k / size) for k in range(bins)]

    def power(self, pairs: Sequence[complex]) -> list[float]:
        spectrum = self._fft.transform(pairs)
        powers = []
        for k, rotation in enumerate(self._rotations):
            current = spectrum[k]
            mirrored = spectrum[-k].conjugate()  # spectrum[size/2 - k], and spectrum[0] for k = 0
            value = (current + mirrored) * 0.5 + rotation * (current - mirrored)
            powers.append(value.real * value.real + value.imag * value.imag)
        return powers


class AdaptiveFloor:
    def __init__(self, *, rise_rate: float, minimum_db: float) -> None:
        self._rise_rate = rise_rate
        self._minimum_db = minimum_db
        self._floor_db = minimum_db

    def track(self, value_db: float) -> float:
        if value_db < self._floor_db:
            self._floor_db = max(self._minimum_db, value_db)
        else:
            self._floor_db += self._rise_rate * (value_db - self._floor_db)
        return self._floor_db


class SpectrumAnalyzer:
    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        window_size: int = WINDOW_SIZE,
        low_hz: float = 60.0,
        high_hz: float = 6000.0,
        dynamic_range_db: float = 36.0,
        gate_db: float = 6.0,
        attack: float = 0.6,
        release: float = 0.25,
        floor_rise_rate: float = 0.004,
        floor_minimum_db: float = -70.0,
    ) -> None:
        if not 0 < attack <= 1 or not 0 < release <= 1:
            raise ValueError("attack and release must be within (0, 1]")
        if dynamic_range_db <= 0:
            raise ValueError("dynamic_range_db must be positive")
        layout = BandLayout(sample_rate, window_size, low_hz, high_hz, BAND_COUNT)
        self._window_size = window_size
        self._ranges = layout.bin_ranges()
        weights = hann_weights(window_size)
        self._even_weights, self._odd_weights = weights[0::2], weights[1::2]
        # parec delivers signed 16-bit samples: scale them while windowing.
        self._even_pcm_weights = [weight / 32768 for weight in self._even_weights]
        self._odd_pcm_weights = [weight / 32768 for weight in self._odd_weights]
        self._spectrum = RealPowerSpectrum(window_size, max(stop for _, stop in self._ranges))
        self._dynamic_range_db = dynamic_range_db
        self._gate_db = gate_db
        self._attack = attack
        self._release = release
        self._floors = [AdaptiveFloor(rise_rate=floor_rise_rate, minimum_db=floor_minimum_db) for _ in self._ranges]
        self._full_scale = (window_size / 4) ** 2
        self._levels = [0.0] * BAND_COUNT

    @property
    def window_size(self) -> int:
        return self._window_size

    def analyze(self, samples: Sequence[float]) -> Levels:
        """Levels for one window of samples in -1..1."""
        if len(samples) != self._window_size:
            raise ValueError(f"expected {self._window_size} samples, got {len(samples)}")
        return self._levels_from(_windowed_pairs(samples, self._even_weights, self._odd_weights))

    def analyze_pcm16(self, chunk: bytes) -> Levels:
        """Levels for one window of signed 16-bit little-endian samples, as parec sends them."""
        if len(chunk) != 2 * self._window_size:
            raise ValueError(f"expected {2 * self._window_size} bytes, got {len(chunk)}")
        return self.analyze_int16(pcm16_samples(chunk))

    def analyze_int16(self, samples: Sequence[int]) -> Levels:
        """Levels for one window of signed 16-bit sample values."""
        if len(samples) != self._window_size:
            raise ValueError(f"expected {self._window_size} samples, got {len(samples)}")
        return self._levels_from(
            _windowed_pairs(samples, self._even_pcm_weights, self._odd_pcm_weights)
        )

    def _levels_from(self, pairs: list[complex]) -> Levels:
        power = self._spectrum.power(pairs)
        for index, (start, stop) in enumerate(self._ranges):
            energy = sum(power[start:stop]) / (stop - start)
            value_db = 10 * math.log10(energy / self._full_scale + 1e-12)
            floor_db = self._floors[index].track(value_db)
            target = (value_db - floor_db - self._gate_db) / self._dynamic_range_db
            target = max(0.0, min(1.0, target))
            rate = self._attack if target > self._levels[index] else self._release
            self._levels[index] += rate * (target - self._levels[index])
        return tuple(self._levels)


def _windowed_pairs(
    samples: Sequence[float], even_weights: Sequence[float], odd_weights: Sequence[float]
) -> list[complex]:
    return [
        complex(even * even_weight, odd * odd_weight)
        for even, odd, even_weight, odd_weight in zip(
            samples[0::2], samples[1::2], even_weights, odd_weights, strict=True
        )
    ]


def pcm16_samples(chunk: bytes) -> array:
    """Signed 16-bit little-endian PCM as sample values (interleaved if it is stereo)."""
    samples = array("h", chunk)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


AUDIO_SOURCES = ("microphone", "system")


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """What to capture: the default microphone, or the system sound (the default output's
    monitor) as one channel or as left and right apart."""

    source: str = "microphone"
    channels: int = 1

    def __post_init__(self) -> None:
        if self.source not in AUDIO_SOURCES:
            raise ValueError(f"audio source must be one of {', '.join(AUDIO_SOURCES)}")
        if self.channels not in (1, 2):
            raise ValueError("channels must be 1 or 2")


@dataclass(frozen=True, slots=True)
class Spectrum:
    """Band levels for the left and the right module; the same tuple twice when mono."""

    left: Levels
    right: Levels

    def side(self, side: str) -> Levels:
        return self.left if side == "left" else self.right


class CaptureProcess(Protocol):
    stdout: IO[bytes] | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessFactory = Callable[[CaptureRequest], CaptureProcess]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]

# PipeWire: capture what the default output plays. Without a target the stream follows the
# default output when it changes (headphones, HDMI).
_PW_CAPTURE_SINK = "{ stream.capture.sink = true }"


def parec_command(
    *, device: str | None, sample_rate: int, channels: int = 1
) -> tuple[str, ...]:
    command = (
        "parec",
        "--raw",
        "--format=s16le",
        f"--rate={sample_rate}",
        f"--channels={channels}",
        "--latency-msec=40",
    )
    return command if device is None else (*command, f"--device={device}")


def pw_record_command(
    *,
    device: str | None,
    sample_rate: int,
    channels: int = 1,
    capture_sink: bool = False,
) -> tuple[str, ...]:
    """PipeWire's own recorder: the same raw stream, for systems without parec."""
    command = (
        "pw-record",
        "--raw",
        "--format=s16",  # native byte order, little-endian on every Framework laptop
        f"--rate={sample_rate}",
        f"--channels={channels}",
        "--latency=40ms",
    )
    if capture_sink:
        command = (*command, "-P", _PW_CAPTURE_SINK)
    return (*command, "-") if device is None else (*command, f"--target={device}", "-")


def capture_command(
    request: CaptureRequest = CaptureRequest(),
    *,
    sample_rate: int,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    """The microphone through parec where it exists (PulseAudio, or PipeWire with its Pulse
    tools), else pw-record. The system sound through pw-record where it exists, because its
    stream follows the default output; else parec on the default output's monitor."""
    channels = request.channels
    if request.source == "system":
        if which("pw-record"):
            return pw_record_command(
                device=None, sample_rate=sample_rate, channels=channels, capture_sink=True
            )
        if which("parec"):
            return parec_command(
                device="@DEFAULT_MONITOR@", sample_rate=sample_rate, channels=channels
            )
    elif which("parec"):
        return parec_command(device=None, sample_rate=sample_rate, channels=channels)
    elif which("pw-record"):
        return pw_record_command(device=None, sample_rate=sample_rate, channels=channels)
    raise FileNotFoundError("neither parec nor pw-record is installed")


def _spawn_capture(request: CaptureRequest) -> CaptureProcess:
    return subprocess.Popen(
        capture_command(request, sample_rate=SAMPLE_RATE),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


class SoundSpectrum:
    """Run parec or pw-record and keep the latest band levels; restart it when it stops.

    Two channels are analysed apart (left and right module); one channel serves both.
    """

    def __init__(
        self,
        *,
        analyzer_factory: Callable[[], SpectrumAnalyzer] = SpectrumAnalyzer,
        process_factory: ProcessFactory = _spawn_capture,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
        stale_after_seconds: float = 1.0,
        restart_initial_seconds: float = 0.5,
        restart_max_seconds: float = 10.0,
    ) -> None:
        self._analyzer_factory = analyzer_factory
        self._process_factory = process_factory
        self._clock = clock
        self._sleeper = sleeper
        self._stale_after_seconds = stale_after_seconds
        self._restart_initial_seconds = restart_initial_seconds
        self._restart_max_seconds = restart_max_seconds
        self._lock = threading.Lock()
        self._latest: Spectrum | None = None
        self._updated_at = -math.inf
        self._running = False
        self._generation = 0  # a capture thread that outlived stop() must not carry on
        self._request: CaptureRequest | None = None
        self._process: CaptureProcess | None = None
        self._thread: threading.Thread | None = None

    @property
    def request(self) -> CaptureRequest | None:
        """What is being captured, or None while stopped."""
        return self._request if self._running else None

    def start(self, request: CaptureRequest = CaptureRequest()) -> None:
        """Start capturing `request`; a capture of something else is stopped first."""
        if self._running:
            if request == self._request:
                return
            self.stop()
        self._request = request
        self._running = True
        self._generation += 1
        self._thread = threading.Thread(
            target=self._run,
            args=(request, self._generation),
            name="sound-spectrum",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._terminate()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            self._latest = None
            self._updated_at = -math.inf

    def levels(self) -> Spectrum | None:
        with self._lock:
            if self._clock() - self._updated_at > self._stale_after_seconds:
                return None
            return self._latest

    def _current(self, generation: int) -> bool:
        return self._running and generation == self._generation

    def _run(self, request: CaptureRequest, generation: int) -> None:
        failures = 0
        while self._current(generation):
            try:
                self._capture_until_exit(request, generation)
                failures = 0
            except (OSError, ValueError):
                pass
            if not self._current(generation):
                break
            failures += 1
            delay = min(self._restart_initial_seconds * 2 ** min(failures - 1, 16), self._restart_max_seconds)
            self._sleeper(delay)

    def _capture_until_exit(self, request: CaptureRequest, generation: int) -> None:
        process = self._process_factory(request)
        self._process = process
        stream = process.stdout
        if stream is None:
            raise OSError("capture process has no stdout")
        analyzers = [self._analyzer_factory() for _ in range(request.channels)]
        chunk_size = analyzers[0].window_size * 2 * request.channels
        try:
            while self._current(generation):
                chunk = stream.read(chunk_size)
                if len(chunk) < chunk_size:
                    raise OSError("capture stream ended")
                if request.channels == 1:
                    levels = analyzers[0].analyze_pcm16(chunk)
                    self._publish(Spectrum(levels, levels), generation)
                else:
                    samples = pcm16_samples(chunk)
                    left, right = analyzers
                    spectrum = Spectrum(
                        left.analyze_int16(samples[0::2]), right.analyze_int16(samples[1::2])
                    )
                    self._publish(spectrum, generation)
        finally:
            self._terminate()

    def _publish(self, spectrum: Spectrum, generation: int) -> None:
        with self._lock:
            if not self._current(generation):
                return
            self._latest = spectrum
            self._updated_at = self._clock()

    def _terminate(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
