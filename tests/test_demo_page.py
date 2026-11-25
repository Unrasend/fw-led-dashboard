from __future__ import annotations

import importlib.util
import json
import random
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from framework_led_dashboard.layout import DEFAULT_LAYOUT
from framework_led_dashboard.panel_render import render_panel
from framework_led_dashboard.weather_codes import SUPPORTED_WEATHER_CODES

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
_spec = importlib.util.spec_from_file_location(
    "build_demo_data", ROOT / "scripts" / "build_demo_data.py"
)
assert _spec and _spec.loader
build_demo_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_demo_data)

REPOSITORY_URL = "https://github.com/Unrasend/fw-led-dashboard"
PIPE_COMMAND = f"wget -qO- {REPOSITORY_URL}/releases/latest/download/install.sh | bash"
NODE_RUNNER = """
import { readFileSync } from "node:fs";
import { DEMO } from "./demo-data.js";
import { MockSpectrum, composeLeft, composeRight, decode } from "./panel.js";

const values = DEMO.pixelValues;
const cases = JSON.parse(readFileSync(0, "utf8"));
const frames = cases.map((c) => {
  const variant = DEMO.icons.find((item) => item.code === c.code)[c.night ? "night" : "day"];
  const icon = variant[c.frame];
  const left = decode(DEMO.timeline.left[c.step], values);
  const right = decode(DEMO.timeline.right[c.step], values);
  return {
    left: Array.from(composeLeft(DEMO, left, c.levels)),
    right: Array.from(composeRight(DEMO, right, decode(icon, values), c.levels)),
  };
});
const spectrum = new MockSpectrum({ bands: DEMO.frameWidth });
const seen = [];
for (let tick = 0; tick < 400; tick += 1) seen.push(...spectrum.advance(0.15));
process.stdout.write(JSON.stringify({ frames, spectrum: [Math.min(...seen), Math.max(...seen)] }));
"""


def test_demo_frames_are_rendered_from_the_current_renderers() -> None:
    expected = build_demo_data.render_module(build_demo_data.build())

    assert (DOCS / "demo-data.js").read_text(encoding="utf-8") == expected, (
        "docs/demo-data.js is stale: run PYTHONPATH=src python3 scripts/build_demo_data.py"
    )


def _cases() -> list[dict[str, object]]:
    rng = random.Random(7)
    edges = [0.0, 1.0, 0.5, -0.3, 1.7, 1 / 22, 3 / 22, 0.999]  # clamps and rounding ties
    cases: list[dict[str, object]] = []
    for code in sorted(SUPPORTED_WEATHER_CODES):
        for night in (False, True):
            for frame in (0, 7, 13):
                levels = [rng.random() for _ in range(9)]
                if frame == 7:
                    levels = [edges[(code + index) % len(edges)] for index in range(9)]
                cases.append(
                    {
                        "code": code,
                        "night": night,
                        "frame": frame,
                        "step": rng.randrange(build_demo_data.TIMELINE_STEPS),
                        "levels": levels,
                    }
                )
    return cases


def _expected(case: dict[str, object]) -> tuple[list[int], list[int]]:
    data = build_demo_data.panel_data(
        case["step"], weather_code=case["code"], is_day=not case["night"]
    )
    left = render_panel(
        DEFAULT_LAYOUT.left, data, spectrum=case["levels"], mirrored=True
    ).snapshot()
    right = render_panel(
        DEFAULT_LAYOUT.right, data, spectrum=case["levels"], weather_frame=case["frame"]
    ).snapshot()
    return [value for row in left for value in row], [value for row in right for value in row]


def test_page_script_composes_the_same_pixels_as_the_python_renderers(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for name in ("panel.js", "demo-data.js"):
        shutil.copy(DOCS / name, tmp_path / name)
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "run.js").write_text(NODE_RUNNER, encoding="utf-8")
    cases = _cases()

    result = subprocess.run(
        [node, "run.js"],
        cwd=tmp_path,
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    output = json.loads(result.stdout)

    assert len(output["frames"]) == len(cases) == len(SUPPORTED_WEATHER_CODES) * 6
    for case, frame in zip(cases, output["frames"], strict=True):
        left, right = _expected(case)
        assert frame["left"] == left, case
        assert frame["right"] == right, case
    low, high = output["spectrum"]
    assert 0 <= low < 0.1 and 0.8 < high <= 1


def test_page_offers_the_installer_command_and_the_current_release() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"{PIPE_COMMAND}</code>" in page
    assert f"{PIPE_COMMAND} -s -- --uninstall</code>" in page
    assert PIPE_COMMAND in readme
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert f"REPOSITORY_URL={REPOSITORY_URL}\n" in script
    assert f'PIPE_COMMAND="{PIPE_COMMAND.replace(REPOSITORY_URL, "$REPOSITORY_URL")}"' in script
    tarball = r"releases/download/v([\d.]+)/framework-led-dashboard-([\d.]+)\.tar\.gz"
    tarballs = set(re.findall(tarball, page))
    assert tarballs == {(version, version)}
    assert f"release {version}" in page
