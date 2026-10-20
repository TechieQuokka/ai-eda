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
    op_via_transient: bool = False  # the operating point came from ngspice's optran


# Noise from the convergence aids ngspice tries before a transient operating point.
_STEPPING = ("gmin stepping failed", "source stepping failed")


@dataclass(frozen=True)
class Plot:
    name: str
    vectors: dict[str, list[float]]  # lower-case vector name → real values
    complex_vectors: dict[str, list[complex]]  # same, for complex (AC) plots


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


def run(netlist: Path, log_path: Path, pspice_mode: bool = False) -> RunOutput:
    """Run ngspice in batch mode, ignoring user init files (`-n`) for reproducibility.

    `pspice_mode` sets `ngbehavior=ps`, which many vendor (PSpice) models need.
    """
    compat = ["-D", "ngbehavior=ps"] if pspice_mode else []
    proc = subprocess.run(
        [executable(), "-b", "-n", *compat, netlist.name],
        cwd=netlist.parent,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    log = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
    log_path.write_text(log, encoding="utf-8")
    errors, warnings, via_transient = classify_log(log)
    return RunOutput(
        returncode=proc.returncode,
        log=log,
        errors=errors,
        warnings=warnings,
        op_via_transient=via_transient,
    )


def classify_log(log: str) -> tuple[list[str], list[str], bool]:
    """(errors, warnings, operating point found by transient) from ngspice output."""
    errors: list[str] = []
    warnings: list[str] = []
    via_transient = False
    for raw in log.splitlines():
        line = raw.removeprefix("stderr ").removeprefix("stdout ").strip()
        lowered = line.lower()
        if lowered.startswith("note: transient op finished successfully"):
            via_transient = True
        elif lowered.startswith("error") or " error:" in lowered or "fatal" in lowered:
            errors.append(line)
        elif lowered.startswith("warning"):
            warnings.append(line)
    if via_transient:
        warnings = [w for w in warnings if not any(s in w.lower() for s in _STEPPING)]
    return errors, warnings, via_transient


def parse_raw(text: str) -> list[Plot]:
    """Parse an ASCII raw file (`set filetype=ascii`) holding real or complex plots."""
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
        is_complex = "complex" in header.get("Flags", "")
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
        vectors: dict[str, list[float]] = {}
        complex_vectors: dict[str, list[complex]] = {}
        columns: dict[str, list[complex]] = {name: [] for name in names}
        for p in range(points):
            row = tokens[p * (count + 1) + 1 : (p + 1) * (count + 1)]
            for name, value in zip(names, row, strict=True):
                real, _, imag = value.partition(",")
                columns[name].append(complex(float(real), float(imag or 0.0)))
        for name, values in columns.items():
            if is_complex and name != "frequency":
                complex_vectors[name] = values
            else:
                vectors[name] = [v.real for v in values]
        plots.append(
            Plot(
                name=header.get("Plotname", ""),
                vectors=vectors,
                complex_vectors=complex_vectors,
            )
        )
    return plots
