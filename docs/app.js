import { DEMO } from "./demo-data.js";
import { MockSpectrum, composeLeft, composeRight, decode } from "./panel.js";

const PAD = 0.6; // empty border around the LEDs, in LED pitches
const LED_COLOR = [255, 247, 232];

const values = DEMO.pixelValues;
const timeline = {
  left: DEMO.timeline.left.map((text) => decode(text, values)),
  right: DEMO.timeline.right.map((text) => decode(text, values)),
};
const icons = DEMO.icons.map((icon) => ({
  ...icon,
  day: icon.day.map((text) => decode(text, values)),
  night: icon.night.map((text) => decode(text, values)),
}));
const iconFrameCount = icons[0].day.length;

class LedCanvas {
  constructor(canvas, columns, rows) {
    this.canvas = canvas;
    this.columns = columns;
    this.rows = rows;
    this.context = canvas.getContext("2d");
    this.pixels = new Uint8Array(columns * rows);
    canvas.style.aspectRatio = `${columns + 2 * PAD} / ${rows + 2 * PAD}`;
    new ResizeObserver(() => this.resize()).observe(canvas);
    this.resize();
  }

  resize() {
    const ratio = window.devicePixelRatio || 1;
    const height = Math.max(1, Math.round(this.canvas.clientHeight * ratio));
    const pitch = height / (this.rows + 2 * PAD);
    const width = Math.round(pitch * (this.columns + 2 * PAD));
    if (width === this.canvas.width && height === this.canvas.height) return;
    this.canvas.width = width;
    this.canvas.height = height;
    this.pitch = pitch;
    this.sprite = ledSprite(pitch);
    this.draw(this.pixels);
  }

  draw(pixels) {
    this.pixels = pixels;
    const { context, pitch, sprite } = this;
    if (!pitch) return;
    context.clearRect(0, 0, this.canvas.width, this.canvas.height);
    context.fillStyle = "rgba(255, 255, 255, 0.055)";
    const offRadius = pitch * 0.26;
    context.beginPath();
    for (let y = 0; y < this.rows; y += 1) {
      for (let x = 0; x < this.columns; x += 1) {
        if (pixels[y * this.columns + x]) continue;
        const cx = (PAD + x + 0.5) * pitch;
        const cy = (PAD + y + 0.5) * pitch;
        context.moveTo(cx + offRadius, cy);
        context.arc(cx, cy, offRadius, 0, Math.PI * 2);
      }
    }
    context.fill();
    const size = sprite.width;
    for (let y = 0; y < this.rows; y += 1) {
      for (let x = 0; x < this.columns; x += 1) {
        const value = pixels[y * this.columns + x];
        if (!value) continue;
        context.globalAlpha = Math.pow(value / 255, 0.75);
        context.drawImage(sprite, (PAD + x + 0.5) * pitch - size / 2, (PAD + y + 0.5) * pitch - size / 2);
      }
    }
    context.globalAlpha = 1;
  }
}

function ledSprite(pitch) {
  const size = Math.max(4, Math.ceil(pitch * 2.4));
  const sprite = document.createElement("canvas");
  sprite.width = sprite.height = size;
  const context = sprite.getContext("2d");
  const center = size / 2;
  const [r, g, b] = LED_COLOR;
  const glow = context.createRadialGradient(center, center, 0, center, center, center);
  glow.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.55)`);
  glow.addColorStop(0.3, `rgba(${r}, ${g}, ${b}, 0.16)`);
  glow.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);
  context.fillStyle = glow;
  context.fillRect(0, 0, size, size);
  context.fillStyle = `rgb(${r}, ${g}, ${b})`;
  context.beginPath();
  context.arc(center, center, pitch * 0.3, 0, Math.PI * 2);
  context.fill();
  return sprite;
}

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const hour = new Date().getHours();
const state = {
  playing: !reducedMotion,
  clock: 0,
  nextPanelFrame: 0,
  iconFrame: -1,
  weather: { code: 81, night: hour < 7 || hour >= 19 },
};

const spectrum = new MockSpectrum({ bands: DEMO.frameWidth });
spectrum.advance(1.2); // start mid-song, so a paused page still shows an equalizer

const left = new LedCanvas(document.getElementById("panel-left"), DEMO.frameWidth, DEMO.frameHeight);
const right = new LedCanvas(document.getElementById("panel-right"), DEMO.frameWidth, DEMO.frameHeight);
const weatherNow = document.getElementById("weather-now");
const gallery = buildGallery(document.getElementById("gallery"));

function currentIcon() {
  return icons.find((icon) => icon.code === state.weather.code);
}

function drawPanels() {
  const step = Math.floor(state.clock / DEMO.timeline.stepSeconds) % timeline.left.length;
  const icon = currentIcon()[state.weather.night ? "night" : "day"][Math.max(0, state.iconFrame)];
  left.draw(composeLeft(DEMO, timeline.left[step], spectrum.levels));
  right.draw(composeRight(DEMO, timeline.right[step], icon, spectrum.levels));
}

function drawGallery() {
  for (const { led, icon, night } of gallery) {
    led.draw(icon[night ? "night" : "day"][Math.max(0, state.iconFrame)]);
  }
}

function selectWeather(code, night) {
  state.weather = { code, night };
  const icon = currentIcon();
  weatherNow.textContent = `${icon.label}, ${night ? "night" : "day"}`;
  for (const { button, icon: item, night: itemNight } of gallery) {
    button.setAttribute("aria-pressed", String(item.code === code && itemNight === night));
  }
  drawPanels();
}

function buildGallery(container) {
  const items = [];
  for (const icon of icons) {
    const card = document.createElement("article");
    card.className = "card";
    const row = document.createElement("div");
    row.className = "icons";
    for (const night of [false, true]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "icon";
      button.setAttribute("aria-label", `Show ${icon.label} by ${night ? "night" : "day"} on the right panel`);
      const canvas = document.createElement("canvas");
      const caption = document.createElement("span");
      caption.textContent = night ? "night" : "day";
      button.append(canvas, caption);
      button.addEventListener("click", () => {
        selectWeather(icon.code, night);
        if (window.matchMedia("(max-width: 1080px)").matches) {
          document.querySelector(".stage").scrollIntoView({ block: "center" });
        }
      });
      row.append(button);
      items.push({ button, canvas, icon, night });
    }
    const title = document.createElement("h3");
    title.textContent = icon.label;
    const code = document.createElement("p");
    code.className = "code";
    code.textContent = `WMO code ${icon.code}`;
    card.append(row, title, code);
    container.append(card);
  }
  return items.map((item) => ({ ...item, led: new LedCanvas(item.canvas, DEMO.iconWidth, DEMO.iconHeight) }));
}

const playButton = document.getElementById("play");
function setPlaying(playing) {
  state.playing = playing;
  playButton.textContent = playing ? "Pause" : "Play";
  playButton.setAttribute("aria-pressed", String(playing));
}
playButton.addEventListener("click", () => setPlaying(!state.playing));

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const text = document.getElementById(button.dataset.copy).textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(document.getElementById(button.dataset.copy));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = "Selected";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1600);
  });
}

let last = performance.now();
function tick(now) {
  const elapsed = Math.min(0.25, (now - last) / 1000);
  last = now;
  if (state.playing) state.clock += elapsed;
  const iconFrame = Math.floor(state.clock * DEMO.weatherFramesPerSecond) % iconFrameCount;
  let panelsDirty = false;
  if (iconFrame !== state.iconFrame) {
    state.iconFrame = iconFrame;
    drawGallery();
    panelsDirty = true;
  }
  if (state.playing && state.clock >= state.nextPanelFrame) {
    spectrum.advance(DEMO.panelFrameSeconds);
    state.nextPanelFrame = state.clock + DEMO.panelFrameSeconds;
    panelsDirty = true;
  }
  if (panelsDirty) drawPanels();
  requestAnimationFrame(tick);
}

setPlaying(state.playing);
selectWeather(state.weather.code, state.weather.night);
requestAnimationFrame(tick);
