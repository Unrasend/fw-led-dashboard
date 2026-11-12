// Pure panel composition for the demo page; tests/test_demo_page.py runs it under node and
// compares the result with the Python renderers pixel by pixel.

export function decode(text, values) {
  const pixels = new Uint8Array(text.length);
  for (let index = 0; index < text.length; index += 1) {
    pixels[index] = values[text[index]];
  }
  return pixels;
}

// equalizer.py: equalizer_bar_heights / draw_equalizer.
export function equalizerHeights(levels, rows) {
  return levels.map((level) => Math.floor(Math.max(0, Math.min(1, level)) * rows + 0.5));
}

export function drawEqualizer(pixels, levels, spec, width, { mirrored = false } = {}) {
  const heights = equalizerHeights(levels, spec.rows);
  const columns = mirrored ? [...heights].reverse() : heights;
  const bottom = spec.top + spec.rows - 1;
  columns.forEach((height, x) => {
    for (let step = 0; step < height; step += 1) {
      pixels[(bottom - step) * width + x] = step === height - 1 ? spec.peak : spec.body;
    }
  });
}

// Left panel: bars from the renderer, spectrum mirrored so the bass meets in the middle.
export function composeLeft(demo, base, levels) {
  const pixels = base.slice();
  if (levels) drawEqualizer(pixels, levels, demo.equalizer, demo.frameWidth, { mirrored: true });
  return pixels;
}

// Right panel: temperatures from the renderer, the weather icon on top (rows 0-9), spectrum.
export function composeRight(demo, text, icon, levels) {
  const pixels = text.slice();
  for (let index = 0; index < icon.length; index += 1) {
    if (icon[index]) pixels[index] = icon[index];
  }
  if (levels) drawEqualizer(pixels, levels, demo.equalizer, demo.frameWidth);
  return pixels;
}

// A made-up song for the equalizer: kick on the beat, hi-hats on the off-beat, a wandering
// melody in the middle, with the attack/release smoothing of audio.py (0.6 / 0.25 per window).
export class MockSpectrum {
  constructor({ bands = 9, random = Math.random } = {}) {
    this.levels = new Array(bands).fill(0);
    this.random = random;
    this.time = 0;
  }

  advance(seconds, windowSeconds = 0.032) {
    for (let elapsed = 0; elapsed < seconds; elapsed += windowSeconds) {
      this.time += windowSeconds;
      this.levels = this.levels.map((level, band) => {
        const target = Math.max(0, Math.min(1, this.target(band)));
        const rate = target > level ? 0.6 : 0.25;
        return level + rate * (target - level);
      });
    }
    return this.levels;
  }

  target(band) {
    const beat = 0.48;
    const bar = Math.floor(this.time / (beat * 4));
    if (bar % 8 === 7 && this.time % (beat * 4) > beat * 2) return 0.04 * this.random(); // a breath
    const onBeat = (this.time % beat) / beat;
    const offBeat = ((this.time + beat / 2) % beat) / beat;
    const accent = Math.floor(this.time / beat) % 4 === 0 ? 1.2 : 1; // first beat of the bar
    const kick = accent * Math.exp(-onBeat * 2.5);
    const hat = Math.exp(-offBeat * 6);
    const jitter = 0.12 * (this.random() - 0.5);
    if (band <= 1) return 0.3 + 0.8 * kick - band * 0.08 + jitter;
    if (band <= 3) return 0.3 + 0.3 * Math.sin(this.time * 1.7 + band) + 0.4 * kick + jitter;
    if (band <= 6) {
      const melody = 0.5 + 0.5 * Math.sin(this.time * 2.3 + band * 1.9) * Math.sin(this.time * 0.6);
      return 0.12 + 0.8 * melody + jitter;
    }
    return 0.1 + 0.75 * hat - (band - 7) * 0.12 + jitter;
  }
}
