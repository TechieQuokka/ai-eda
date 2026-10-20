"""SVG → PNG so an AI agent can look at rendered images.

Uses `rsvg-convert` when installed, otherwise headless Chrome/Chromium with a private
profile directory inside the project's `out/`.
"""

import re
import shutil
import subprocess
from pathlib import Path

TIMEOUT_S = 60
_CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def available() -> str | None:
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
        raise RuntimeError("no rasterizer found (install librsvg2-bin or Chrome/Chromium)")
    png.unlink(missing_ok=True)
    if tool == "rsvg-convert":
        cmd = ["rsvg-convert", "-z", "2", "-b", "white", "-o", str(png), str(svg)]
    else:
        width, height = _size(svg.read_text(encoding="utf-8"))
        cmd = [
            tool,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--hide-scrollbars",
            f"--user-data-dir={profile_dir}",
            "--force-device-scale-factor=2",
            "--default-background-color=ffffffff",
            f"--window-size={width},{height}",
            f"--screenshot={png}",
            svg.resolve().as_uri(),
        ]
    subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S, check=False)
    if not png.is_file():
        raise RuntimeError(f"{tool} did not produce {png.name}")
    return tool


def _size(svg_text: str) -> tuple[int, int]:
    root = re.search(r"<svg\b[^>]*>", svg_text)
    attrs = root.group(0) if root else ""
    width = re.search(r'\bwidth="([\d.]+)', attrs)
    height = re.search(r'\bheight="([\d.]+)', attrs)
    w = round(float(width.group(1))) if width else 800
    h = round(float(height.group(1))) if height else 600
    return w, h
