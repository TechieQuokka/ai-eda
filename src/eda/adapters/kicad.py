"""Thin wrappers around KiCad: the pcbnew Python API (via KiCad's own Python) and kicad-cli."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).with_name("pcbnew_script.py")
TIMEOUT_S = 300
_MARKER = "@@EDA-JSON@@"
# Layers a two-layer board house needs.
FAB_LAYERS = "F.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,F.Paste,B.Paste,Edge.Cuts"


class KicadError(RuntimeError):
    pass


def python() -> str:
    """KiCad's Python: the interpreter that can import `pcbnew` (system python3)."""
    found = os.environ.get("EDA_KICAD_PYTHON") or "/usr/bin/python3"
    if not Path(found).is_file():
        raise KicadError(f"KiCad Python not found at {found} (set EDA_KICAD_PYTHON)")
    return found


def cli() -> str:
    found = os.environ.get("EDA_KICAD_CLI") or shutil.which("kicad-cli")
    if not found:
        raise KicadError("kicad-cli not found on PATH (set EDA_KICAD_CLI)")
    return found


def pcbnew(request: dict[str, Any]) -> dict[str, Any]:
    """Run one pcbnew_script command and return its result object."""
    proc = subprocess.run(
        [python(), str(SCRIPT)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(_MARKER):
            reply: dict[str, Any] = json.loads(line.removeprefix(_MARKER))
            if not reply.get("ok"):
                raise KicadError(str(reply.get("error")))
            result: dict[str, Any] = reply["result"]
            return result
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    raise KicadError("pcbnew script failed: " + " | ".join(tail))


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [cli(), *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=False, cwd=cwd
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise KicadError(f"kicad-cli {' '.join(args[:3])} failed: " + " | ".join(detail))
    return proc


def drc(pcb: Path, report: Path) -> dict[str, Any]:
    # Exit code 5 means "violations found"; the report is still written.
    proc = subprocess.run(
        [cli(), "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm",
         "--output", str(report), str(pcb)],
        capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
    )  # fmt: skip
    if not report.is_file():
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise KicadError("kicad-cli pcb drc failed: " + " | ".join(detail))
    data: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
    return data


def export_fabrication(pcb: Path, out: Path) -> list[Path]:
    """Gerbers, Excellon drill files, and a component position file into `out/`."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():
        if old.is_file():
            old.unlink()
    run_cli("pcb", "export", "gerbers", "--layers", FAB_LAYERS, "--output", f"{out}/", str(pcb))
    run_cli("pcb", "export", "drill", "--output", f"{out}/", str(pcb))
    run_cli(
        "pcb", "export", "pos", "--format", "csv", "--units", "mm",
        "--output", str(out / f"{pcb.stem}-pos.csv"), str(pcb),
    )  # fmt: skip
    return sorted(p for p in out.iterdir() if p.is_file())


def export_svg(pcb: Path, svg: Path) -> None:
    run_cli(
        "pcb", "export", "svg", "--mode-single", "--page-size-mode", "2",
        "--exclude-drawing-sheet", "--layers", "B.Cu,F.Cu,F.Silkscreen,Edge.Cuts",
        "--output", str(svg), str(pcb),
    )  # fmt: skip


def render_3d(pcb: Path, png: Path, side: str = "top") -> None:
    run_cli(
        "pcb", "render", "--side", side, "--width", "1200", "--height", "800",
        "--background", "opaque", "--quality", "basic", "--output", str(png), str(pcb),
    )  # fmt: skip
