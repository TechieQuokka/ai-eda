"""Thin wrapper around netlistsvg (Node), pinned in `tools/netlistsvg/`."""

import json
import shutil
import subprocess
from pathlib import Path

from pydantic import JsonValue

from eda.project import REPO_ROOT

TOOL_DIR = REPO_ROOT / "tools" / "netlistsvg"
PACKAGE_DIR = TOOL_DIR / "node_modules" / "netlistsvg"
TIMEOUT_S = 60


class NetlistsvgUnavailable(RuntimeError):
    pass


def render(netlist: dict[str, JsonValue], json_path: Path, svg_path: Path, skin: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise NetlistsvgUnavailable("node not found on PATH (needed for netlistsvg)")
    if not (PACKAGE_DIR / "bin" / "netlistsvg.js").is_file():
        raise NetlistsvgUnavailable(f"netlistsvg is not installed; run: npm --prefix {TOOL_DIR} ci")
    json_path.write_text(json.dumps(netlist, indent=1), encoding="utf-8")
    svg_path.unlink(missing_ok=True)
    proc = subprocess.run(
        [
            node,
            str(PACKAGE_DIR / "bin" / "netlistsvg.js"),
            str(json_path),
            "--skin",
            str(skin),
            "-o",
            str(svg_path),
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0 or not svg_path.is_file():
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise RuntimeError("netlistsvg failed: " + (detail[-1] if detail else "no output"))
