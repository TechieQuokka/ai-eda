"""Resolved circuit → ngspice netlist with a batch `.control` block."""

import re
from pathlib import Path

from eda.model import GROUND, Ac, Analysis, Op, ResolvedCircuit, ResolvedPart

_PLACEHOLDER = re.compile(r"<(name|model|pin:[^>]+|param:[^>]+)>")


def node_name(net: str) -> str:
    return "0" if net == GROUND else net.lower()


def model_name(part: ResolvedPart) -> str:
    param = part.spec.spice.model_param
    assert param is not None
    choice = str(part.values[param])
    return re.sub(r"[^a-z0-9_]", "_", f"{part.spec.id}_{choice}".lower())


def _sense(part: ResolvedPart, pin: str) -> tuple[str, str]:
    """(sense source name, internal node) for a pin measured through a 0 V source."""
    index = part.spec.pins.index(pin)
    tag = f"{part.ref}_{index}".lower()
    return f"v_s_{tag}", f"s_{tag}"


def sensed_pins(part: ResolvedPart) -> list[str]:
    """Pins measured through a 0 V source: every pin of a multi-pin part, else the first.

    Sense sources work in every analysis (unlike `.option savecurrents`, which has no AC
    currents for devices inside subcircuits), and give one sign convention: current flowing
    from the net into the pin.
    """
    return list(part.spec.pins) if part.spec.spice.pin_currents else [part.spec.pins[0]]


def element_lines(part: ResolvedPart, circuit: ResolvedCircuit) -> list[str]:
    """The part's element line, preceded by its pin sense sources."""
    lines: list[str] = []
    sensed = sensed_pins(part)

    def pin_node(pin: str) -> str:
        net = circuit.net_of(part.ref, pin)
        if net is None:
            raise ValueError(f"{part.ref}.{pin} is not connected")
        if pin not in sensed:
            return node_name(net)
        source, inner = _sense(part, pin)
        lines.append(f"{source} {node_name(net)} {inner} DC 0")
        return inner

    def substitute(match: re.Match[str]) -> str:
        token = match[1]
        if token == "name":
            return part.spice_name
        if token == "model":
            return model_name(part)
        kind, _, key = token.partition(":")
        if kind == "pin":
            return pin_node(key)
        value = part.values[key]
        return value if isinstance(value, str) else f"{value:.12g}"

    template = " ".join(part.spec.spice.template.split())
    lines.append(_PLACEHOLDER.sub(substitute, template))
    return lines


def current_vector(part: ResolvedPart) -> str | None:
    """Raw-file name of a two-terminal part's current (into its first pin)."""
    if part.spec.spice.pin_currents:
        return None
    return pin_current_vector(part, part.spec.pins[0])


def pin_current_vector(part: ResolvedPart, pin: str) -> str:
    """Raw-file name of the current flowing from the net into `pin`."""
    return f"i({_sense(part, pin)[0]})"


def voltage_vector(net: str) -> str | None:
    """Raw-file vector for a net voltage; None for ground (always 0 V)."""
    return None if net == GROUND else f"v({net.lower()})"


def needs_pspice_mode(circuit: ResolvedCircuit) -> bool:
    return any(part.spec.spice.compat == "ps" for part in circuit.parts.values())


def build_netlist(title: str, circuit: ResolvedCircuit, analysis: Analysis, raw_path: Path) -> str:
    lines = [f"* {title}"]
    includes: list[str] = []
    models: dict[str, str] = {}
    for part in circuit.parts.values():
        include = part.spec.include_path()
        if include is not None and include not in includes:
            includes.append(include)
        lines.extend(element_lines(part, circuit))
        param = part.spec.spice.model_param
        if param is not None:
            models[model_name(part)] = part.spec.spice.models[str(part.values[param])]
    lines[1:1] = [f'.include "{path}"' for path in includes]
    lines.extend(f".model {name} {body}" for name, body in sorted(models.items()))

    if isinstance(analysis, Op):
        command = "op"
    elif isinstance(analysis, Ac):
        command = f"ac dec {analysis.points_per_decade} {analysis.start:.12g} {analysis.stop:.12g}"
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
