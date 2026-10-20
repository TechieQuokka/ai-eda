"""SVG → PNG so an AI agent can look at rendered images.

Prefers CairoSVG (in-process, fast; needs the system cairo library), then `rsvg-convert`,
then headless Chrome/Chromium with a private profile directory inside the project's `out/`.
"""

import importlib
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

TIMEOUT_S = 60
SCALE = 2
_CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


@cache
def _cairosvg() -> Any:  # noqa: ANN401 - CairoSVG ships without type information
    try:
        return importlib.import_module("cairosvg")
    except (ImportError, OSError):  # OSError: cairo shared library missing
        return None


def available() -> str | None:
    if _cairosvg() is not None:
        return "cairosvg"
    if shutil.which("rsvg-convert"):
        return "rsvg-convert"
    for name in _CHROMES:
        if shutil.which(name):
            return name
    return None


def svg_to_png(svg: Path, png: Path, profile_dir: Path) -> str:
    """Rasterize `svg` at 2x scale on a white background. Returns the tool used."""
    tool = available()
    if tool is None:
        raise RuntimeError("no rasterizer found (install cairo, librsvg2-bin, or Chrome)")
    png.unlink(missing_ok=True)
    if tool == "cairosvg":
        _cairosvg().svg2png(url=str(svg), write_to=str(png), scale=SCALE, background_color="white")
    else:
        subprocess.run(
            _command(tool, svg, png, profile_dir),
            capture_output=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    if not png.is_file():
        raise RuntimeError(f"{tool} did not produce {png.name}")
    return tool


def _command(tool: str, svg: Path, png: Path, profile_dir: Path) -> list[str]:
    if tool == "rsvg-convert":
        return ["rsvg-convert", "-z", str(SCALE), "-b", "white", "-o", str(png), str(svg)]
    width, height = _size(svg.read_text(encoding="utf-8"))
    return [
        tool,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--hide-scrollbars",
        f"--user-data-dir={profile_dir}",
        f"--force-device-scale-factor={SCALE}",
        "--default-background-color=ffffffff",
        f"--window-size={width},{height}",
        f"--screenshot={png}",
        svg.resolve().as_uri(),
    ]


def _size(svg_text: str) -> tuple[int, int]:
    root = re.search(r"<svg\b[^>]*>", svg_text)
    attrs = root.group(0) if root else ""
    width = re.search(r'\bwidth="([\d.]+)', attrs)
    height = re.search(r'\bheight="([\d.]+)', attrs)
    w = round(float(width.group(1))) if width else 800
    h = round(float(height.group(1))) if height else 600
    return w, h
