"""Thin wrapper around the ngspice executable and its ASCII raw file format."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_S = 120


class NgspiceNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class RunOutput:
    returncode: int
    log: str
    errors: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class Plot:
    name: str
    vectors: dict[str, list[float]]  # lower-case vector name → values


def executable() -> str:
    found = os.environ.get("EDA_NGSPICE") or shutil.which("ngspice")
    if not found:
        raise NgspiceNotFound("ngspice not found on PATH (set EDA_NGSPICE to override)")
    return found


def version() -> str | None:
    try:
        out = subprocess.run(
            [executable(), "-v"], capture_output=True, text=True, timeout=10, check=False
        )
    except (NgspiceNotFound, OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.splitlines():
        if "ngspice-" in line:
            return line.strip("* ").split(":")[0].strip()
    return None


def run(netlist: Path, log_path: Path) -> RunOutput:
    """Run ngspice in batch mode, ignoring user init files (`-n`) for reproducibility."""
    proc = subprocess.run(
        [executable(), "-b", "-n", netlist.name],
        cwd=netlist.parent,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    log = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
    log_path.write_text(log, encoding="utf-8")
    errors: list[str] = []
    warnings: list[str] = []
    for raw in log.splitlines():
        line = raw.strip()
        lowered = line.lower()
        if lowered.startswith("error") or " error:" in lowered or "fatal" in lowered:
            errors.append(line)
        elif lowered.startswith("warning"):
            warnings.append(line)
    return RunOutput(returncode=proc.returncode, log=log, errors=errors, warnings=warnings)


def parse_raw(text: str) -> list[Plot]:
    """Parse an ASCII raw file (`set filetype=ascii`) holding one or more real plots."""
    plots: list[Plot] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].startswith("Title:"):
            i += 1
            continue
        header: dict[str, str] = {}
        while i < len(lines) and not lines[i].startswith("Variables:"):
            key, _, value = lines[i].partition(":")
            header[key.strip()] = value.strip()
            i += 1
        if "complex" in header.get("Flags", ""):
            raise ValueError("complex raw data is not supported")
        count = int(header["No. Variables"])
        points = int(header["No. Points"])
        names = [lines[i + 1 + k].split()[1].lower() for k in range(count)]
        i += 1 + count
        if not lines[i].startswith("Values:"):
            raise ValueError(f"expected 'Values:' at line {i + 1}")
        i += 1
        tokens: list[str] = []
        needed = points * (count + 1)  # each point starts with its index
        while len(tokens) < needed and i < len(lines):
            tokens.extend(lines[i].split())
            i += 1
        if len(tokens) < needed:
            raise ValueError("raw file ended early")
        vectors: dict[str, list[float]] = {name: [] for name in names}
        for p in range(points):
            row = tokens[p * (count + 1) + 1 : (p + 1) * (count + 1)]
            for name, value in zip(names, row, strict=True):
                vectors[name].append(float(value))
        plots.append(Plot(name=header.get("Plotname", ""), vectors=vectors))
    return plots
