"""Import vendor SPICE models (`.lib` / `.mod` / `.sub` with `.subckt`) as parts.

Imported files live in `parts/vendor/` (git-ignored): vendor model licenses usually
allow use but not redistribution.
"""

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from eda.adapters import ngspice
from eda.project import VENDOR_PARTS_DIR

OPAMP_ROLES = ("in+", "in-", "v+", "v-", "out")
_SUBCKT = re.compile(r"^\s*\.subckt\s+(\S+)\s*(.*)$", re.IGNORECASE)
_ENDS = re.compile(r"^\s*\.ends\b", re.IGNORECASE)
_PIN_SAFE = re.compile(r"^[^.\s]+$")
# ngspice messages that mean the model text did not load (as opposed to convergence trouble).
_LOAD_ERRORS = ("netlist", "no such", "unknown", "syntax", "undefined", "fatal", "not found")


@dataclass
class Subckt:
    name: str
    nodes: list[str]
    params: dict[str, str]
    line: int  # 1-based
    comments: list[str] = field(default_factory=lambda: list[str]())  # header comment block


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def inspect(text: str) -> list[Subckt]:
    """Top-level subcircuits with their nodes, default params, and nearby comments."""
    lines = _logical_lines(text)
    found: list[Subckt] = []
    depth = 0
    for index, (number, line) in enumerate(lines):
        match = _SUBCKT.match(line)
        if match:
            depth += 1
            if depth > 1:
                continue
            nodes: list[str] = []
            params: dict[str, str] = {}
            rest = match[2].replace("params:", " ").replace("PARAMS:", " ")
            for token in rest.split():
                if "=" in token:
                    key, _, value = token.partition("=")
                    params[key] = value
                elif not params:
                    nodes.append(token)
            found.append(Subckt(match[1], nodes, params, number, _comments_near(lines, index)))
        elif _ENDS.match(line):
            depth = max(0, depth - 1)
    return found


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join `+` continuation lines; keep comment lines (`*`) separately."""
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("+") and out and not out[-1][1].lstrip().startswith("*"):
            out[-1] = (out[-1][0], out[-1][1] + " " + raw[1:])
        else:
            out.append((number, raw))
    return out


def _comments_near(lines: list[tuple[int, str]], index: int) -> list[str]:
    """Comment lines directly above and below the .subckt line (pin descriptions)."""
    above: list[str] = []
    i = index - 1
    while i >= 0 and lines[i][1].lstrip().startswith("*") and len(above) < 12:
        above.insert(0, lines[i][1].strip())
        i -= 1
    below: list[str] = []
    i = index + 1
    while i < len(lines) and lines[i][1].lstrip().startswith("*") and len(below) < 12:
        below.append(lines[i][1].strip())
        i += 1
    return [c for c in above + below if c.strip("* ")]


@dataclass(frozen=True)
class ImportPlan:
    part_id: str
    subckt: Subckt
    pins: list[str]  # part pin for each subckt node, in node order
    category: str
    opamp: bool
    compat: str | None


def plan_import(
    file: Path,
    part_id: str,
    subckt_name: str | None,
    opamp_nodes: list[str] | None,
    category: str | None,
) -> ImportPlan:
    subckts = inspect(read_text(file))
    if not subckts:
        raise ValueError(f"{file.name}: no .subckt found")
    if subckt_name is None:
        if len(subckts) > 1:
            names = ", ".join(s.name for s in subckts)
            raise ValueError(f"{file.name} has several subcircuits ({names}); pass --subckt")
        chosen = subckts[0]
    else:
        matches = [s for s in subckts if s.name.lower() == subckt_name.lower()]
        if not matches:
            raise ValueError(f"no subcircuit {subckt_name!r} in {file.name}")
        chosen = matches[0]

    if opamp_nodes is not None:
        if len(opamp_nodes) != 5:
            raise ValueError("--opamp needs 5 node names: IN+,IN-,V+,V-,OUT")
        lowered = [n.lower() for n in chosen.nodes]
        if len(chosen.nodes) != 5 or sorted(n.lower() for n in opamp_nodes) != sorted(lowered):
            raise ValueError(f"--opamp nodes {opamp_nodes} must be a permutation of {chosen.nodes}")
        role = {n.lower(): r for n, r in zip(opamp_nodes, OPAMP_ROLES, strict=True)}
        pins = [role[n.lower()] for n in chosen.nodes]
    else:
        pins = [_pin_name(n) for n in chosen.nodes]
        if len(set(pins)) != len(pins):
            raise ValueError(f"node names {chosen.nodes} are not unique as pin names")
    return ImportPlan(
        part_id=part_id,
        subckt=chosen,
        pins=pins,
        category=category or ("opamp" if opamp_nodes else "ic"),
        opamp=opamp_nodes is not None,
        compat=detect_compat(file, chosen),
    )


def _pin_name(node: str) -> str:
    return node if _PIN_SAFE.match(node) else re.sub(r"[.\s]", "_", node)


def detect_compat(file: Path, subckt: Subckt) -> str | None:
    """Try the model in plain ngspice, then in PSpice mode; return the mode that loads.

    Each node is tied to ground through 1 MΩ so the operating point is defined.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        nodes = " ".join(f"n{i}" for i in range(len(subckt.nodes)))
        ties = "\n".join(f"R{i} n{i} 0 1e6" for i in range(len(subckt.nodes)))
        (work / "probe.cir").write_text(
            f'* probe\n.include "{file.resolve()}"\nX1 {nodes} {subckt.name}\n{ties}\n'
            ".control\nop\nquit\n.endc\n.end\n",
            encoding="utf-8",
        )
        for mode in (None, "ps"):
            try:
                output = ngspice.run(work / "probe.cir", work / "probe.log", mode == "ps")
            except (ngspice.NgspiceNotFound, subprocess.TimeoutExpired):
                return "ps"
            loading = [e for e in output.errors if any(k in e.lower() for k in _LOAD_ERRORS)]
            if not loading:
                return mode
    return "ps"


def write_part(
    plan: ImportPlan, source: Path, name: str | None, dest: Path = VENDOR_PARTS_DIR
) -> Path:
    """Copy the model into <dest>/models/ and write <dest>/<id>.yaml (dest: parts/vendor)."""
    models = dest / "models"
    models.mkdir(parents=True, exist_ok=True)
    target = models / source.name
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)

    sub = plan.subckt
    pin_tokens = " ".join(f"<pin:{pin}>" for pin in plan.pins)
    if plan.opamp:
        cell = "op"
        ports = {"in+": "+", "in-": "-", "out": "OUT", "v+": "VCC", "v-": "VEE"}
    else:
        cell = f"x_{plan.part_id}"  # unknown cell type → netlistsvg draws a labeled box
        ports = {pin: pin for pin in plan.pins}
    doc = {
        "id": plan.part_id,
        "name": name or sub.name,
        "category": plan.category,
        "description": f"Vendor model {sub.name} imported from {source.name} "
        f"(.subckt at line {sub.line}). Pin currents: current(REF.PIN).",
        "pins": plan.pins,
        "spice": {
            "prefix": "X",
            "template": f"<name> {pin_tokens} {sub.name}",
            "pin_currents": True,
            "include": f"models/{source.name}",
            **({"compat": plan.compat} if plan.compat else {}),
        },
        "symbol": {"cell": cell, "ports": ports, "label": sub.name},
    }
    out = dest / f"{plan.part_id}.yaml"
    header = "# Generated by `eda model import`; vendor file license applies to the model.\n"
    out.write_text(header + _yaml(doc), encoding="utf-8")
    return out


def _yaml(value: object, indent: int = 0) -> str:
    """Tiny YAML emitter: block mappings, flow lists, JSON-quoted strings (valid YAML)."""
    pad = "  " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in cast(dict[str, object], value).items():
            if isinstance(item, dict):
                nested = _yaml(cast(object, item), indent + 1)
                lines.append(f"{pad}{json.dumps(key)}:\n{nested}")
            else:
                lines.append(f"{pad}{json.dumps(key)}: {_scalar(item)}")
        return "\n".join(lines) + ("\n" if indent == 0 else "")
    return f"{pad}{_scalar(value)}"


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(v) for v in cast(list[object], value)) + "]"
    return json.dumps(value)
