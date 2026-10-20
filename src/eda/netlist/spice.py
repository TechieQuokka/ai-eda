"""Resolved circuit → ngspice netlist with a batch `.control` block."""

import re
from pathlib import Path

from eda.model import GROUND, Ac, Analysis, Op, ResolvedCircuit, ResolvedPart

_PLACEHOLDER = re.compile(r"<(name|model|pin:[^>]+|param:[^>]+|choice:[^>]+)>")


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


# Simulated behavior of an MCU pin, set by firmware.yaml (and test overrides).
type Drive = str  # "high" | "low" | "pullup" | "pulldown" | "hiz"


def element_lines(
    part: ResolvedPart, circuit: ResolvedCircuit, drives: dict[str, Drive] | None = None
) -> list[str]:
    """The part's element line(s), preceded by its pin sense sources."""
    if part.spec.spice.generator == "mcu":
        return mcu_lines(part, circuit, drives or {})
    lines: list[str] = []
    sensed = sensed_pins(part)
    nodes: dict[str, str] = {}

    def pin_node(pin: str) -> str:
        if pin in nodes:  # a pin may appear on several template lines
            return nodes[pin]
        net = circuit.net_of(part.ref, pin)
        if net is None:
            raise ValueError(f"{part.ref}.{pin} is not connected")
        if pin not in sensed:
            nodes[pin] = node_name(net)
        else:
            source, inner = _sense(part, pin)
            lines.append(f"{source} {node_name(net)} {inner} DC 0")
            nodes[pin] = inner
        return nodes[pin]

    def substitute(match: re.Match[str]) -> str:
        token = match[1]
        if token == "name":
            return part.spice_name
        if token == "model":
            return model_name(part)
        kind, _, key = token.partition(":")
        if kind == "pin":
            return pin_node(key)
        if kind == "choice":
            return part.spec.spice.choice_values[key][str(part.values[key])]
        value = part.values[key]
        return value if isinstance(value, str) else f"{value:.12g}"

    for template in part.spec.spice.template.split("\n"):
        if template.strip():
            lines.append(_PLACEHOLDER.sub(substitute, " ".join(template.split())))
    return lines


def mcu_lines(part: ResolvedPart, circuit: ResolvedCircuit, drives: dict[str, Drive]) -> list[str]:
    """A static model of a microcontroller's pins, driven by the firmware pin configuration.

    The internal I/O supply is an ideal `vio` source; the supply pin is that source behind
    1 Ω. Each connected GPIO is an output (`vio`/0 V behind the driver resistance), a pull-up
    or pull-down, or high impedance (100 MΩ to ground so the node stays defined).
    """
    mcu = part.spec.mcu
    assert mcu is not None
    tag = part.ref.lower()
    lines: list[str] = []
    nodes: dict[str, str] = {}
    for pin in part.spec.pins:
        net = circuit.net_of(part.ref, pin)
        if net is None:
            continue
        source, inner = _sense(part, pin)
        lines.append(f"{source} {node_name(net)} {inner} DC 0")
        nodes[pin] = inner
    gnd = nodes.get(mcu.ground_pin)
    if gnd is None:
        raise ValueError(f"{part.ref}.{mcu.ground_pin} must be connected")
    vdd = f"{tag}_vdd"
    lines.append(f"V_{tag}_vdd {vdd} {gnd} DC {mcu.vio:.12g}")
    if mcu.supply_pin in nodes:
        lines.append(f"R_{tag}_vout {vdd} {nodes[mcu.supply_pin]} 1")
    for index, (pin, node) in enumerate(nodes.items()):
        name = f"{tag}_{index}"
        if pin in mcu.pins_pulled_up:
            lines.append(f"R_{name}_pu {node} {vdd} {mcu.pull_ohm:.12g}")
        if pin in mcu.loads:
            lines.append(f"R_{name}_load {node} {gnd} {mcu.loads[pin]:.12g}")
        if pin not in mcu.gpio:
            continue
        drive = drives.get(pin, "hiz")
        if drive in ("high", "low"):
            level = mcu.vio if drive == "high" else 0.0
            lines.append(f"V_{name}_drv {name}_d {gnd} DC {level:.12g}")
            lines.append(f"R_{name}_drv {name}_d {node} {mcu.drive_ohm:.12g}")
        elif drive == "pullup":
            lines.append(f"R_{name}_pu {node} {vdd} {mcu.pull_ohm:.12g}")
        elif drive == "pulldown":
            lines.append(f"R_{name}_pd {node} {gnd} {mcu.pull_ohm:.12g}")
        else:
            lines.append(f"R_{name}_hiz {node} {gnd} 1e8")
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


def build_netlist(
    title: str,
    circuit: ResolvedCircuit,
    analysis: Analysis,
    raw_path: Path,
    drives: dict[str, dict[str, Drive]] | None = None,
) -> str:
    """`drives` maps an MCU part reference to its pin drives (from firmware.yaml)."""
    lines = [f"* {title}"]
    includes: list[str] = []
    models: dict[str, str] = {}
    for part in circuit.parts.values():
        include = part.spec.include_path()
        if include is not None and include not in includes:
            includes.append(include)
        lines.extend(element_lines(part, circuit, (drives or {}).get(part.ref)))
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
