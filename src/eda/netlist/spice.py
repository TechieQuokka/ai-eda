"""Resolved circuit → ngspice netlist with a batch `.control` block."""

import re
from pathlib import Path

from eda.model import GROUND, Op, ResolvedCircuit, ResolvedPart, Tran

_PLACEHOLDER = re.compile(r"<(name|model|pin:[^>]+|param:[^>]+)>")


def node_name(net: str) -> str:
    return "0" if net == GROUND else net.lower()


def model_name(part: ResolvedPart) -> str:
    param = part.spec.spice.model_param
    assert param is not None
    choice = str(part.values[param])
    return re.sub(r"[^a-z0-9_]", "_", f"{part.spec.id}_{choice}".lower())


def element_line(part: ResolvedPart, circuit: ResolvedCircuit) -> str:
    def substitute(match: re.Match[str]) -> str:
        token = match[1]
        if token == "name":
            return part.spice_name
        if token == "model":
            return model_name(part)
        kind, _, key = token.partition(":")
        if kind == "pin":
            net = circuit.net_of(part.ref, key)
            if net is None:
                raise ValueError(f"{part.ref}.{key} is not connected")
            return node_name(net)
        value = part.values[key]
        return value if isinstance(value, str) else f"{value:.12g}"

    template = " ".join(part.spec.spice.template.split())
    return _PLACEHOLDER.sub(substitute, template)


def current_vector(part: ResolvedPart) -> str:
    """Name of the part's current in the raw file, e.g. `i(@r1[i])` or `i(v1)`."""
    probe = part.spec.spice.current.replace("<name>", part.spice_name)
    return f"i({probe.lower()})"


def voltage_vector(net: str) -> str | None:
    """Raw-file vector for a net voltage; None for ground (always 0 V)."""
    return None if net == GROUND else f"v({net.lower()})"


def build_netlist(title: str, circuit: ResolvedCircuit, analysis: Op | Tran, raw_path: Path) -> str:
    lines = [f"* {title}"]
    models: dict[str, str] = {}
    for part in circuit.parts.values():
        lines.append(element_line(part, circuit))
        param = part.spec.spice.model_param
        if param is not None:
            models[model_name(part)] = part.spec.spice.models[str(part.values[param])]
    lines.extend(f".model {name} {body}" for name, body in sorted(models.items()))
    lines.append(".option savecurrents")

    if isinstance(analysis, Op):
        command = "op"
    else:
        command = f"tran {analysis.step:.12g} {analysis.stop:.12g}"
        if analysis.start is not None:
            command += f" {analysis.start:.12g}"
    lines += [
        ".control",
        "set filetype=ascii",
        command,
        f"write {raw_path.as_posix()} all",
        "quit",
        ".endc",
        ".end",
    ]
    return "\n".join(lines) + "\n"
