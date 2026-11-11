from __future__ import annotations

import math
import subprocess
import threading
import time
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


class HannWindow:
    def __init__(self, size: int) -> None:
        self._weights = [0.5 - 0.5 * math.cos(2 * math.pi * n / size) for n in range(size)]

    def apply(self, samples: Sequence[float]) -> list[complex]:
        return [complex(sample * weight) for sample, weight in zip(samples, self._weights)]


class RadixTwoFft:
    def __init__(self, size: int) -> None:
        if size < 2 or size & (size - 1):
            raise ValueError("fft size must be a power of two")
        self._size = size
        self._twiddles = [complex(math.cos(-2 * math.pi * k / size), math.sin(-2 * math.pi * k / size)) for k in range(size // 2)]
        bits = size.bit_length() - 1
        self._order = [int(f"{index:0{bits}b}"[::-1], 2) for index in range(size)]

    def power(self, values: Sequence[complex]) -> list[float]:
        data = [values[index] for index in self._order]
        span = 2
        while span <= self._size:
            half = span // 2
            step = self._size // span
            for start in range(0, self._size, span):
                for offset in range(half):
                    twiddle = self._twiddles[offset * step] * data[start + offset + half]
                    data[start + offset + half] = data[start + offset] - twiddle
                    data[start + offset] += twiddle
            span *= 2
        return [abs(value) ** 2 for value in data[: self._size // 2]]


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
        self._window = HannWindow(window_size)
        self._fft = RadixTwoFft(window_size)
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
        if len(samples) != self._window_size:
            raise ValueError(f"expected {self._window_size} samples, got {len(samples)}")
        power = self._fft.power(self._window.apply(samples))
        for index, (start, stop) in enumerate(self._ranges):
            energy = sum(power[start:stop]) / (stop - start)
            value_db = 10 * math.log10(energy / self._full_scale + 1e-12)
            floor_db = self._floors[index].track(value_db)
            target = (value_db - floor_db - self._gate_db) / self._dynamic_range_db
            target = max(0.0, min(1.0, target))
            rate = self._attack if target > self._levels[index] else self._release
            self._levels[index] += rate * (target - self._levels[index])
        return tuple(self._levels)


class CaptureProcess(Protocol):
    stdout: IO[bytes] | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessFactory = Callable[[], CaptureProcess]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def parec_command(*, device: str | None, sample_rate: int) -> tuple[str, ...]:
    command = ("parec", "--raw", "--format=s16le", f"--rate={sample_rate}", "--channels=1", "--latency-msec=40")
    return command if device is None else (*command, f"--device={device}")


def _spawn_parec(device: str | None, sample_rate: int) -> CaptureProcess:
    return subprocess.Popen(
        parec_command(device=device, sample_rate=sample_rate),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


class MicrophoneSpectrum:
    def __init__(
        self,
        *,
        analyzer: SpectrumAnalyzer | None = None,
        process_factory: ProcessFactory | None = None,
        device: str | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
        stale_after_seconds: float = 1.0,
        restart_initial_seconds: float = 0.5,
        restart_max_seconds: float = 10.0,
    ) -> None:
        self._analyzer = analyzer or SpectrumAnalyzer()
        self._process_factory = process_factory or (lambda: _spawn_parec(device, SAMPLE_RATE))
        self._clock = clock
        self._sleeper = sleeper
        self._stale_after_seconds = stale_after_seconds
        self._restart_initial_seconds = restart_initial_seconds
        self._restart_max_seconds = restart_max_seconds
        self._lock = threading.Lock()
        self._latest: Levels | None = None
        self._updated_at = -math.inf
        self._running = False
        self._process: CaptureProcess | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="microphone-spectrum", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._terminate()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def levels(self) -> Levels | None:
        with self._lock:
            if self._clock() - self._updated_at > self._stale_after_seconds:
                return None
            return self._latest

    def _run(self) -> None:
        failures = 0
        while self._running:
            try:
                self._capture_until_exit()
                failures = 0
            except (OSError, ValueError):
                pass
            if not self._running:
                break
            failures += 1
            delay = min(self._restart_initial_seconds * 2 ** min(failures - 1, 16), self._restart_max_seconds)
            self._sleeper(delay)

    def _capture_until_exit(self) -> None:
        process = self._process_factory()
        self._process = process
        stream = process.stdout
        if stream is None:
            raise OSError("capture process has no stdout")
        chunk_size = self._analyzer.window_size * 2
        try:
            while self._running:
                chunk = stream.read(chunk_size)
                if len(chunk) < chunk_size:
                    raise OSError("capture stream ended")
                samples = [int.from_bytes(chunk[i : i + 2], "little", signed=True) / 32768 for i in range(0, chunk_size, 2)]
                self._publish(self._analyzer.analyze(samples))
        finally:
            self._terminate()

    def _publish(self, levels: Levels) -> None:
        with self._lock:
            self._latest = levels
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
