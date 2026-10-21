"""Resolved circuit → ngspice netlist with a batch `.control` block."""

import ast
import math
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from eda.model import GROUND, Ac, Analysis, Dc, Op, ResolvedCircuit, ResolvedPart
from eda.schema.part import SegmentDisplaySpec

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
# "fw": driven by the emulated firmware during co-simulation (see `fw_driver`).
type Drive = str  # "high" | "low" | "pullup" | "pulldown" | "hiz" | "fw"


def element_lines(
    part: ResolvedPart, circuit: ResolvedCircuit, drives: dict[str, Drive] | None = None
) -> list[str]:
    """The part's element line(s), preceded by its pin sense sources."""
    if part.spec.spice.generator == "mcu":
        return mcu_lines(part, circuit, drives or {})
    lines: list[str] = []
    sensed = sensed_pins(part)
    nodes: dict[str, str] = {}
    template = part.spec.spice.template
    if isinstance(part.spec.display, SegmentDisplaySpec):
        connected = {pin for pin in part.spec.pins if circuit.net_of(part.ref, pin) is not None}
        template = segment_template(part.spec.display, connected)

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
            param = key.partition(".")[0]
            return part.spec.spice.choice_values[key][str(part.values[param])]
        value = part.values[key]
        return value if isinstance(value, str) else f"{value:.12g}"

    optional = set(part.spec.optional_pins)
    for line in template.split("\n"):
        if not line.strip():
            continue
        pins = re.findall(r"<pin:([^>]+)>", line)
        if any(pin in optional and circuit.net_of(part.ref, pin) is None for pin in pins):
            continue  # the element hangs on a pin that is left open on purpose
        lines.append(_PLACEHOLDER.sub(substitute, " ".join(line.split())))
    return lines


def segment_template(display: SegmentDisplaySpec, connected: set[str]) -> str:
    """One LED per digit and segment, each behind a 0 V sense source, as a part template;
    LEDs on an unconnected (optional) pin are left out.

    Digit 1's segment a is `<name>_1a`; its current is `segment_vector(part, 1, "a")`."""
    lines: list[str] = []
    for index, common in enumerate(display.digits, start=1):
        for segment, pin in display.segments.items():
            if pin not in connected or common not in connected:
                continue
            anode, cathode = (pin, common) if display.common == "cathode" else (common, pin)
            tag = f"<name>_{index}{segment}"
            lines.append(f"V{tag} <pin:{anode}> {tag}n DC 0")
            lines.append(f"{tag} {tag}n <pin:{cathode}> <model>")
    return "\n".join(lines)


def segment_vector(part: ResolvedPart, digit: int, segment: str) -> str:
    """Raw-file name of the current through one LED of a segment display (anode → cathode)."""
    return f"i(v{part.spice_name}_{digit}{segment})".lower()


def mcu_lines(part: ResolvedPart, circuit: ResolvedCircuit, drives: dict[str, Drive]) -> list[str]:
    """A static model of a microcontroller's pins, driven by the firmware pin configuration.

    The internal I/O supply is an ideal `vio` source; the supply pin is that source behind
    1 Ω. Each connected GPIO is an output (`vio`/0 V behind the driver resistance), a pull-up
    or pull-down, or high impedance (100 MΩ to ground so the node stays defined). A "fw"
    pin is a Thevenin source (voltage source + resistor) that co-simulation alters.
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
        elif drive == "fw":
            source, resistor = fw_driver(part, pin, nodes)
            lines.append(f"{source} {name}_f {gnd} DC 0")
            lines.append(f"{resistor} {name}_f {node} 1e8")
        else:
            lines.append(f"R_{name}_hiz {node} {gnd} 1e8")
    return lines


def fw_driver(
    part: ResolvedPart, pin: str, connected: dict[str, str] | list[str]
) -> tuple[str, str]:
    """(voltage source, resistor) element names of a firmware-driven MCU pin.

    `connected` lists the part's connected pins in pin order (as mcu_lines numbers them).
    """
    index = list(connected).index(pin)
    name = f"{part.ref.lower()}_{index}"
    return f"V_{name}_fw", f"R_{name}_fw"


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


DEFAULT_OPTRAN_STEP = 100e-9


def optran_stop(circuit: ResolvedCircuit) -> float | None:
    """The transient operating-point run time parts ask for (`spice.optran`), if any."""
    specs = [p.spec.spice.optran for p in circuit.parts.values() if p.spec.spice.optran]
    return max(spec.seconds[1] for spec in specs) if specs else None


def optran_command(circuit: ResolvedCircuit, stop: float | None = None) -> str | None:
    """`optran` control command for a transient operating point of `stop` seconds
    (default: what the parts ask for; None when nothing asks).

    Without it ngspice still falls back to a short (10 us) transient operating point after
    gmin and source stepping fail; parts set `spice.optran` to go there directly and to
    give their own settling time. With several, the smallest step and longest stop win.
    """
    specs = [p.spec.spice.optran for p in circuit.parts.values() if p.spec.spice.optran]
    stop = stop if stop is not None else optran_stop(circuit)
    if stop is None:
        return None
    step = min([spec.seconds[0] for spec in specs] or [DEFAULT_OPTRAN_STEP])
    step = min(step, stop / 100)
    # no initial Newton iteration, no gmin or source stepping, no supply ramp
    return f"optran 0 0 0 {step:.12g} {stop:.12g} 0"


def build_netlist(
    title: str,
    circuit: ResolvedCircuit,
    analysis: Analysis,
    raw_path: Path,
    drives: dict[str, dict[str, Drive]] | None = None,
    batch: bool = True,
    optran: float | None = None,
) -> str:
    """`drives` maps an MCU part reference to its pin drives (from firmware.yaml).

    `batch=False` leaves out the `.control` block, for interactive (co-simulation) runs.
    `optran` forces a transient operating point of that many seconds.
    """
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
    elif isinstance(analysis, Dc):
        command = ""  # one operating point per value, below
    elif isinstance(analysis, Ac):
        command = f"ac dec {analysis.points_per_decade} {analysis.start:.12g} {analysis.stop:.12g}"
    else:
        command = f"tran {analysis.step:.12g} {analysis.stop:.12g}"
        if analysis.start is not None:
            command += f" {analysis.start:.12g}"
    if batch:
        optran_line = optran_command(circuit, optran)
        if isinstance(analysis, Dc):
            # every point appends its own plot to the raw file
            runs = ["set appendwrite", *sweep_commands(circuit, analysis, raw_path)]
        else:
            runs = [command, f"write {raw_path.as_posix()} all"]
        lines += [
            ".control",
            "set filetype=ascii",
            *([optran_line] if optran_line else []),
            *runs,
            "quit",
            ".endc",
        ]
    lines.append(".end")
    return "\n".join(lines) + "\n"


def sweep_commands(circuit: ResolvedCircuit, sweep: Dc, raw_path: Path) -> list[str]:
    """Set the swept parameter to each value in turn, solve the operating point, and write it."""
    part = circuit.parts[sweep.ref]
    current = part  # what the elements hold now: alter from the previous point
    commands: list[str] = []
    for value in sweep.values:
        changed = replace(part, values={**part.values, sweep.param: value})
        commands += [*alter_commands(current, changed, circuit), "op"]
        commands.append(f"write {raw_path.as_posix()} all")
        current = changed
    return commands


# --- changing a part during a run ----------------------------------------------------------

_ALTERABLE = frozenset("RCL")
_BINARY: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def _max(*values: float) -> float:
    return max(values)


def _min(*values: float) -> float:
    return min(values)


def _abs(value: float) -> float:
    return abs(value)


def _pow(base: float, exponent: float) -> float:
    if base < 0 and not float(exponent).is_integer():
        raise ValueError(f"pow({base}, {exponent}) has no real value")
    return float(base**exponent)


def _exp(value: float) -> float:
    return math.exp(value)


def _sqrt(value: float) -> float:
    return math.sqrt(value)


# The ngspice expression functions part templates use (same names, same meaning).
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "max": _max,
    "min": _min,
    "abs": _abs,
    "pow": _pow,
    "exp": _exp,
    "sqrt": _sqrt,
}


def alter_commands(old: ResolvedPart, new: ResolvedPart, circuit: ResolvedCircuit) -> list[str]:
    """ngspice `alter` commands that turn `old`'s elements into `new`'s mid-simulation.

    Only R, C, and L values (`R1 a b VALUE`) and the DC value of independent sources
    (`V1 a b DC VALUE ...`) can change; a value may be a `{...}` expression of numbers,
    + - * /, max, min, abs, pow, exp, and sqrt. Raises ValueError otherwise.
    """
    before = element_lines(old, circuit)
    after = element_lines(new, circuit)
    if len(before) != len(after):
        raise ValueError("the parameter changes the part's SPICE structure")
    commands: list[str] = []
    for a, b in zip(before, after, strict=True):
        if a == b:
            continue
        source = _source_dc_change(a, b)
        if source is not None:
            commands.append(source)
            continue
        tokens_a, tokens_b = a.split(maxsplit=3), b.split(maxsplit=3)
        name = tokens_b[0]
        if len(tokens_b) != 4 or name[0].upper() not in _ALTERABLE or tokens_a[:3] != tokens_b[:3]:
            raise ValueError(
                f"only R, C, and L values and source DC values can change during a run, not {b!r}"
            )
        commands.append(f"alter {name} = {_spice_number(tokens_b[3]):.12g}")
    return commands


def _source_dc_change(before: str, after: str) -> str | None:
    """`alter V1 dc = VALUE` when two source lines differ only in the value after DC."""
    tokens_a, tokens_b = before.split(), after.split()
    if tokens_b[0][0].upper() not in "VI" or len(tokens_a) != len(tokens_b):
        return None
    changed = [i for i, (x, y) in enumerate(zip(tokens_a, tokens_b, strict=True)) if x != y]
    if len(changed) != 1 or changed[0] < 1 or tokens_b[changed[0] - 1].upper() != "DC":
        return None
    return f"alter {tokens_b[0]} dc = {_spice_number(tokens_b[changed[0]]):.12g}"


def _spice_number(text: str) -> float:
    if text.startswith("{") and text.endswith("}"):
        return _evaluate(ast.parse(text[1:-1], mode="eval").body)
    return float(text)


def _evaluate(node: ast.expr) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(node.operand)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return _BINARY[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
    ):
        return _FUNCTIONS[node.func.id](*(_evaluate(arg) for arg in node.args))
    raise ValueError(f"cannot evaluate {ast.unparse(node)!r}")
