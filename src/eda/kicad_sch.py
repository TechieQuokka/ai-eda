"""KiCad schematic (`.kicad_sch`) export: the circuit as a KiCad 10 schematic.

Symbols come from the KiCad libraries (`kicad:` in each part, with fallbacks); parts
without one get a generated box symbol. Connections are drawn the way KiCad users label
busy sheets: every pin gets a short wire and a net label, GND gets ground symbols, and
unused pins get no-connect flags. Placement is a simple left-to-right flow in the order
of `circuit.yaml`, so the file is readable and stable, not a hand-drawn schematic.

The output is checked by re-exporting the netlist with kicad-cli (`eda render`).
"""

import copy
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from functools import cache
from itertools import pairwise
from pathlib import Path

from pydantic import JsonValue

from eda.adapters import kicad, rasterize
from eda.model import GROUND, Design, ResolvedPart
from eda.result import Diagnostics
from eda.sexpr import Atom, SExpr, child, children, dumps, head, number, parse

SYMBOL_DIR = Path(os.environ.get("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols"))
VERSION = "20260306"  # KiCad 10 schematic format
GRID = 2.54
STUB = 2.54  # wire from a pin to its label
TEXT = 1.27  # font size
OPAMP_PINS = {"in+": ["1"], "in-": ["2"], "v+": ["3"], "v-": ["4"], "out": ["5"]}
PAPERS = (("A4", 297.0, 210.0), ("A3", 420.0, 297.0), ("A2", 594.0, 420.0), ("A1", 841.0, 594.0))
BORDER = 15.0  # keep clear of the drawing-sheet frame

type Box = tuple[float, float, float, float]  # x0, y0, x1, y1


class SymbolError(ValueError):
    pass


@dataclass(frozen=True)
class Pin:
    number: str
    x: float  # connection point, library coordinates (y up)
    y: float
    angle: float  # direction from the connection point into the body
    kind: str = "passive"  # electrical type: passive, power_in, power_out, input, ...


@dataclass(frozen=True)
class LibSymbol:
    lib_id: str
    tree: list[SExpr]  # flattened definition named `lib_id`, for `lib_symbols`
    pins: tuple[Pin, ...]
    box: Box  # body and pins, library coordinates


@dataclass
class Schematic:
    text: str
    paper: str
    expected: dict[str, set[tuple[str, str]]]  # net → {(reference, symbol pin)}
    symbols: dict[str, str] = field(default_factory=lambda: dict[str, str]())  # ref → lib_id


# --- library symbols ---------------------------------------------------------------------


@cache
def _library(name: str) -> list[SExpr]:
    path = SYMBOL_DIR / f"{name}.kicad_sym"
    if not path.is_file():
        raise SymbolError(f"KiCad symbol library {name!r} not found in {SYMBOL_DIR}")
    return parse(path.read_text(encoding="utf-8"))


def _find(library: list[SExpr], name: str) -> list[SExpr] | None:
    return next((s for s in children(library, "symbol") if s[1] == name), None)


def load_symbol(lib_id: str) -> LibSymbol:
    library_name, _, name = lib_id.partition(":")
    library = _library(library_name)
    found = _find(library, name)
    if found is None:
        raise SymbolError(f"no symbol {name!r} in library {library_name!r}")
    tree = copy.deepcopy(found)
    extends = child(tree, "extends")
    if extends is not None:  # derived symbol: parent's drawing, own properties
        parent = _find(library, str(extends[1]))
        if parent is None:
            raise SymbolError(f"{lib_id} extends a missing symbol {extends[1]!r}")
        base = copy.deepcopy(parent)
        own = {str(p[1]): p for p in children(tree, "property")}
        merged: list[SExpr] = [Atom("symbol"), name]
        for item in base[2:]:
            if head(item) == "property" and isinstance(item, list) and str(item[1]) in own:
                merged.append(own.pop(str(item[1])))
            elif head(item) == "symbol" and isinstance(item, list):
                renamed = list(item)
                renamed[1] = name + str(item[1])[len(str(parent[1])) :]
                merged.append(renamed)
            else:
                merged.append(item)
        merged.extend(own.values())
        tree = merged
    tree[1] = lib_id
    pins: list[Pin] = []
    points: list[tuple[float, float]] = []
    for unit in children(tree, "symbol"):
        match = re.fullmatch(r".*_(\d+)_(\d+)", str(unit[1]))
        if match is None or match[1] not in ("0", "1") or match[2] not in ("0", "1"):
            continue  # other units and De Morgan styles are not placed
        for pin in children(unit, "pin"):
            at, length = child(pin, "at"), child(pin, "length")
            num = child(pin, "number")
            if at is None or num is None:
                continue
            x, y, angle = number(at[1]), number(at[2]), number(at[3]) if len(at) > 3 else 0.0
            pins.append(Pin(str(num[1]), x, y, angle, str(pin[1])))
            reach = number(length[1]) if length is not None else 0.0
            rad = math.radians(angle)
            points += [(x, y), (x + reach * math.cos(rad), y + reach * math.sin(rad))]
        points += _graphic_points(unit)
    if not points:
        points = [(0.0, 0.0)]
    box = (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )
    return LibSymbol(lib_id, tree, tuple(pins), box)


def _graphic_points(unit: list[SExpr]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for item in unit:
        kind = head(item)
        if kind == "rectangle" or kind == "arc":
            for key in ("start", "mid", "end"):
                found = child(item, key)
                if found is not None:
                    points.append((number(found[1]), number(found[2])))
        elif kind == "polyline" or kind == "bezier":
            for xy in children(child(item, "pts") or [], "xy"):
                points.append((number(xy[1]), number(xy[2])))
        elif kind == "circle":
            center, radius = child(item, "center"), child(item, "radius")
            if center is not None and radius is not None:
                cx, cy, r = number(center[1]), number(center[2]), number(radius[1])
                points += [(cx - r, cy - r), (cx + r, cy + r)]
    return points


def box_symbol(part_id: str, pins: list[str]) -> LibSymbol:
    """A plain rectangle with the part's pins: first half on the left, rest on the right."""
    left = pins[: (len(pins) + 1) // 2]
    right = pins[len(left) :]
    rows = max(len(left), len(right))
    width = 10.16
    top = (rows - 1) * GRID / 2
    lib_id = f"eda:{part_id}"
    body: list[SExpr] = [
        Atom("rectangle"),
        [Atom("start"), Atom(f"{-width / 2:g}"), Atom(f"{top + GRID:g}")],
        [Atom("end"), Atom(f"{width / 2:g}"), Atom(f"{-top - GRID:g}")],
        [Atom("stroke"), [Atom("width"), Atom("0.254")], [Atom("type"), Atom("default")]],
        [Atom("fill"), [Atom("type"), Atom("background")]],
    ]
    unit: list[SExpr] = [Atom("symbol"), f"{part_id}_1_1", body]
    placed: list[Pin] = []
    for side, names in ((-1, left), (1, right)):
        for row, name in enumerate(names):
            x = side * (width / 2 + GRID)
            y = top - row * GRID
            angle = 0.0 if side < 0 else 180.0
            num = str(pins.index(name) + 1)
            placed.append(Pin(num, x, y, angle))
            unit.append(
                [
                    Atom("pin"),
                    Atom("passive"),
                    Atom("line"),
                    [Atom("at"), Atom(f"{x:g}"), Atom(f"{y:g}"), Atom(f"{angle:g}")],
                    [Atom("length"), Atom(f"{GRID:g}")],
                    [Atom("name"), name, _effects()],
                    [Atom("number"), num, _effects()],
                ]
            )
    tree: list[SExpr] = [
        Atom("symbol"),
        lib_id,
        [Atom("exclude_from_sim"), Atom("no")],
        [Atom("in_bom"), Atom("yes")],
        [Atom("on_board"), Atom("yes")],
        _property("Reference", "U", 0, top + GRID * 2, hide=False),
        _property("Value", part_id, 0, -top - GRID * 2, hide=False),
        _property("Footprint", "", 0, 0, hide=True),
        _property("Datasheet", "", 0, 0, hide=True),
        _property("Description", f"eda part {part_id}", 0, 0, hide=True),
        unit,
    ]
    half = width / 2 + (GRID if left else 0.0)
    box = (-half - GRID, -top - GRID, width / 2 + (GRID if right else 0.0), top + GRID)
    return LibSymbol(lib_id, tree, tuple(placed), box)


def symbol_for(part: ResolvedPart) -> tuple[LibSymbol, dict[str, list[str]]]:
    """The symbol for a part and part pin → symbol pin numbers."""
    spec = part.spec
    pads: dict[str, list[str]] = {}
    if spec.pcb is not None:
        pads = {pin: [p] if isinstance(p, str) else list(p) for pin, p in spec.pcb.pads.items()}
    if spec.kicad is not None:
        mapping = pads
        if spec.kicad.pins is not None:
            mapping = {
                pin: [p] if isinstance(p, str) else list(p) for pin, p in spec.kicad.pins.items()
            }
        return load_symbol(spec.kicad.symbol), mapping
    if spec.category == "opamp" and set(spec.pins) == set(OPAMP_PINS):
        return load_symbol("Simulation_SPICE:OPAMP"), dict(OPAMP_PINS)
    symbol = box_symbol(spec.id, list(spec.pins))
    return symbol, {pin: [str(i + 1)] for i, pin in enumerate(spec.pins)}


# --- writing -----------------------------------------------------------------------------


def _effects(justify: str | None = None, hide: bool = False) -> list[SExpr]:
    effects: list[SExpr] = [
        Atom("effects"),
        [Atom("font"), [Atom("size"), Atom(f"{TEXT:g}"), Atom(f"{TEXT:g}")]],
    ]
    if justify:
        effects.append([Atom("justify"), *(Atom(w) for w in justify.split())])
    if hide:
        effects.append([Atom("hide"), Atom("yes")])
    return effects


def _property(
    name: str, value: str, x: float, y: float, hide: bool, justify: str | None = None
) -> list[SExpr]:
    return [
        Atom("property"),
        name,
        value,
        [Atom("at"), Atom(_mm(x)), Atom(_mm(y)), Atom("0")],
        _effects(justify, hide),
    ]


def _mm(value: float) -> str:
    text = f"{round(value, 4):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _snap(value: float) -> float:
    return round(value / GRID) * GRID


# outward direction of a pin in schematic coordinates (y down), by library pin angle
_OUTWARD = {0: (-1.0, 0.0), 90: (0.0, 1.0), 180: (1.0, 0.0), 270: (0.0, -1.0)}
# label angle and justification for each outward direction
_LABEL = {
    (1.0, 0.0): (0, "left bottom"),
    (-1.0, 0.0): (180, "right bottom"),
    (0.0, -1.0): (90, "left bottom"),
    (0.0, 1.0): (270, "right bottom"),
}


class _Writer:
    def __init__(self, design: Design) -> None:
        self.design = design
        self.name = design.project.name
        self.root = self._uuid("root")
        self.items: list[SExpr] = []
        self.lib: dict[str, list[SExpr]] = {}
        self.power = 0

    def _uuid(self, key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"eda:{self.name}:{key}"))

    def _instances(self, reference: str) -> list[SExpr]:
        return [
            Atom("instances"),
            [
                Atom("project"),
                self.name,
                [
                    Atom("path"),
                    f"/{self.root}",
                    [Atom("reference"), reference],
                    [Atom("unit"), Atom("1")],
                ],
            ],
        ]

    def symbol(
        self,
        sym: LibSymbol,
        ref: str,
        value: str,
        footprint: str,
        x: float,
        y: float,
        rotation: int = 0,
        power: bool = False,
    ) -> None:
        self.lib.setdefault(sym.lib_id, sym.tree)
        node: list[SExpr] = [
            Atom("symbol"),
            [Atom("lib_id"), sym.lib_id],
            [Atom("at"), Atom(_mm(x)), Atom(_mm(y)), Atom(str(rotation))],
            [Atom("unit"), Atom("1")],
            [Atom("exclude_from_sim"), Atom("no")],
            [Atom("in_bom"), Atom("no" if power else "yes")],
            [Atom("on_board"), Atom("no" if power else "yes")],
            [Atom("dnp"), Atom("no")],
            [Atom("uuid"), self._uuid(f"symbol:{ref}")],
            self._field(sym, "Reference", ref, x, y, rotation, hide=power),
            self._field(sym, "Value", value, x, y, rotation),
            _property("Footprint", footprint, x, y, True),
            _property("Datasheet", "", x, y, True),
            _property("Description", "", x, y, True),
        ]
        for pin in sym.pins:
            node.append(
                [Atom("pin"), pin.number, [Atom("uuid"), self._uuid(f"{ref}:{pin.number}")]]
            )
        node.append(self._instances(ref))
        self.items.append(node)

    def _field(
        self,
        sym: LibSymbol,
        name: str,
        value: str,
        x: float,
        y: float,
        rotation: int,
        hide: bool = False,
    ) -> list[SExpr]:
        """A symbol field where the library symbol puts it (designers place these to clear
        the pins), turned with the symbol."""
        found = next((p for p in children(sym.tree, "property") if p[1] == name), None)
        at = child(found, "at") if found is not None else None
        px, py = (number(at[1]), number(at[2])) if at is not None else (0.0, 0.0)
        if rotation == 180:
            px, py = -px, -py
        justify: str | None = None
        effects = child(found, "effects") if found is not None else None
        just = child(effects, "justify") if effects is not None else None
        if just is not None:
            words = [str(w) for w in just[1:]]
            if rotation == 180:
                flip = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
                words = [flip.get(w, w) for w in words]
            justify = " ".join(words)
        angle = number(at[3]) if at is not None and len(at) > 3 else 0.0
        field_node = _property(name, value, x + px, y - py, hide, justify)
        field_node[3] = [Atom("at"), Atom(_mm(x + px)), Atom(_mm(y - py)), Atom(_mm(angle))]
        return field_node

    def wire(self, points: list[tuple[float, float]], key: str) -> None:
        """A path as KiCad wires (each wire is one straight segment)."""
        for i, ((ax, ay), (bx, by)) in enumerate(pairwise(points)):
            self.items.append(
                [
                    Atom("wire"),
                    [
                        Atom("pts"),
                        [Atom("xy"), Atom(_mm(ax)), Atom(_mm(ay))],
                        [Atom("xy"), Atom(_mm(bx)), Atom(_mm(by))],
                    ],
                    [Atom("stroke"), [Atom("width"), Atom("0")], [Atom("type"), Atom("default")]],
                    [Atom("uuid"), self._uuid(f"wire:{key}:{i}")],
                ]
            )

    def label(self, net: str, x: float, y: float, outward: tuple[float, float], key: str) -> None:
        angle, justify = _LABEL[outward]
        self.items.append(
            [
                Atom("label"),
                net,
                [Atom("at"), Atom(_mm(x)), Atom(_mm(y)), Atom(str(angle))],
                [Atom("fields_autoplaced"), Atom("yes")],
                _effects(justify),
                [Atom("uuid"), self._uuid(f"label:{key}")],
            ]
        )

    def ground(self, x: float, y: float, up: bool) -> None:
        self.power += 1
        ref = f"#PWR{self.power:02d}"
        self.symbol(load_symbol("power:GND"), ref, "GND", "", x, y, 180 if up else 0, power=True)

    def flag(self, x: float, y: float) -> None:
        self.power += 1
        ref = f"#FLG{self.power:02d}"
        self.symbol(load_symbol("power:PWR_FLAG"), ref, "PWR_FLAG", "", x, y, power=True)

    def no_connect(self, x: float, y: float, key: str) -> None:
        self.items.append(
            [
                Atom("no_connect"),
                [Atom("at"), Atom(_mm(x)), Atom(_mm(y))],
                [Atom("uuid"), self._uuid(f"nc:{key}")],
            ]
        )

    def document(self, paper: str) -> str:
        lib_symbols: list[SExpr] = [Atom("lib_symbols"), *self.lib.values()]
        tree: list[SExpr] = [
            Atom("kicad_sch"),
            [Atom("version"), Atom(VERSION)],
            [Atom("generator"), "eda"],
            [Atom("generator_version"), "0.0"],
            [Atom("uuid"), self.root],
            [Atom("paper"), paper],
            [
                Atom("title_block"),
                [Atom("title"), self.name],
                [Atom("comment"), Atom("1"), "Generated by eda from design/circuit.yaml"],
            ],
            lib_symbols,
            *self.items,
            [Atom("sheet_instances"), [Atom("path"), "/", [Atom("page"), "1"]]],
            [Atom("embedded_fonts"), Atom("no")],
        ]
        return dumps(tree) + "\n"


def _label_width(text: str) -> float:
    return len(text) * TEXT * 0.8 + 2.0


def _flag_step(net: str) -> float:
    """Room for one PWR_FLAG ("PWR_FLAG" is drawn centred above it) and its label."""
    label = 0.0 if net == GROUND else STUB + _label_width(net)
    return max(_label_width("PWR_FLAG") + GRID, label + GRID * 2)


def build(design: Design) -> Schematic:
    """The schematic of `design.circuit` as `.kicad_sch` text."""
    circuit = design.circuit
    board_footprints = {p.ref: p.footprint for p in design.board.parts} if design.board else {}
    placed: list[tuple[ResolvedPart, LibSymbol, dict[str, list[str]], float, float]] = []
    expected: dict[str, set[tuple[str, str]]] = {}
    symbols: dict[str, str] = {}

    # Sizes with room for the labels around each symbol.
    layout: list[tuple[ResolvedPart, LibSymbol, dict[str, list[str]], Box]] = []
    for part in circuit.parts.values():
        sym, mapping = symbol_for(part)
        longest = max((len(net) for net in circuit.nets), default=4)
        room = STUB + _label_width("x" * longest) + GRID
        x0, y0, x1, y1 = sym.box
        layout.append((part, sym, mapping, (x0 - room, -y1 - room, x1 + room, -y0 + room)))

    # Nets a source drives get a PWR_FLAG so KiCad's ERC knows they are powered, unless a
    # symbol pin on the net already is a power output (e.g. a module's GND pins).
    driven: set[str] = set()
    sourced: set[str] = set()
    for part, sym, mapping, _ in layout:
        kinds = {pin.number: pin.kind for pin in sym.pins}
        for pin, numbers in mapping.items():
            net = circuit.net_of(part.ref, pin)
            if net is None:
                continue
            if part.spec.category == "source":
                sourced.add(net)
            if any(kinds.get(num) == "power_out" for num in numbers):
                driven.add(net)
    flagged: list[str] = sorted(sourced - driven, key=lambda net: (net != GROUND, net))
    flag_width = sum(_flag_step(net) for net in flagged)
    boxes = [box for *_, box in layout]
    if flagged:
        boxes.append((0.0, -GRID * 2, flag_width, GRID * 3))

    paper = PAPERS[-1][0]
    positions: list[tuple[float, float]] = []
    for name, width, height in PAPERS:
        positions = _flow(boxes, width - 2 * BORDER)
        bottom = max(
            (py + box[3] for (_, py), box in zip(positions, boxes, strict=True)), default=0.0
        )
        if bottom <= height - 2 * BORDER:
            paper = name
            break

    writer = _Writer(design)
    if flagged:
        fx, fy = positions[-1]
        x, y = _snap(fx + BORDER + GRID), _snap(fy + BORDER)
        for net in flagged:
            writer.flag(x, y)
            if net == GROUND:
                writer.ground(x, y, up=False)
            else:
                writer.wire([(x, y), (x + STUB, y)], f"flag:{net}")
                writer.label(net, x + STUB, y, (1.0, 0.0), f"flag:{net}")
            x += _snap(_flag_step(net))
    for (part, sym, mapping, _), (px, py) in zip(layout, positions, strict=False):
        x, y = _snap(px + BORDER), _snap(py + BORDER)
        placed.append((part, sym, mapping, x, y))
        symbols[part.ref] = sym.lib_id
        footprint = board_footprints.get(part.ref) or (
            part.spec.pcb.footprint if part.spec.pcb else ""
        )
        writer.symbol(sym, part.ref, part.label() or part.spec.id, footprint, x, y)

    for part, sym, mapping, x, y in placed:
        number_net: dict[str, str] = {}
        for pin, numbers in mapping.items():
            net = circuit.net_of(part.ref, pin)
            for num in numbers:
                if net is not None:
                    number_net[num] = net
                    expected.setdefault(net, set()).add((part.ref, num))
        done: set[tuple[float, float]] = set()
        for pin in sym.pins:
            px, py = x + pin.x, y - pin.y
            if (round(px, 3), round(py, 3)) in done:
                continue  # stacked pins (e.g. a module's GND pins) share one connection
            done.add((round(px, 3), round(py, 3)))
            key = f"{part.ref}:{pin.number}"
            net = number_net.get(pin.number)
            if net is None:
                writer.no_connect(px, py, key)
                continue
            dx, dy = _OUTWARD.get(int(pin.angle) % 360, (1.0, 0.0))
            ex, ey = px + dx * STUB, py + dy * STUB
            if net == GROUND:
                if dy < 0:  # pin on top: ground symbol upside down
                    writer.wire([(px, py), (ex, ey)], key)
                    writer.ground(ex, ey, up=True)
                elif dy > 0:
                    writer.wire([(px, py), (ex, ey)], key)
                    writer.ground(ex, ey, up=False)
                else:
                    writer.wire([(px, py), (ex, ey), (ex, ey + STUB)], key)
                    writer.ground(ex, ey + STUB, up=False)
            else:
                writer.wire([(px, py), (ex, ey)], key)
                writer.label(net, ex, ey, (dx, dy), key)
    return Schematic(writer.document(paper), paper, expected, symbols)


def _flow(boxes: list[Box], width: float) -> list[tuple[float, float]]:
    """Top-left positions for boxes laid out left to right in rows of `width`."""
    positions: list[tuple[float, float]] = []
    x = y = row = 0.0
    for x0, y0, x1, y1 in boxes:
        w, h = x1 - x0, y1 - y0
        if x > 0 and x + w > width:
            x, y, row = 0.0, y + row, 0.0
        positions.append((x - x0, y - y0))
        x += w
        row = max(row, h)
    return positions


def netlist_nets(text: str) -> dict[str, set[tuple[str, str]]]:
    """Nets of a kicad-cli `kicadsexpr` netlist: name → {(reference, pin)}; names lose the
    sheet prefix (`/VCC` → `VCC`)."""
    tree = parse(text)
    nets: dict[str, set[tuple[str, str]]] = {}
    for net in children(child(tree, "nets") or [], "net"):
        name_node = child(net, "name")
        name = str(name_node[1]).lstrip("/") if name_node else ""
        if name.startswith("unconnected-"):
            continue  # a pin with a no-connect flag
        nodes = {
            (str(r[1]), str(p[1]))
            for node in children(net, "node")
            if (r := child(node, "ref")) is not None and (p := child(node, "pin")) is not None
            and not str(r[1]).startswith("#")
        }  # fmt: skip
        if nodes:
            nets[name] = nodes
    return nets


def compare(
    expected: dict[str, set[tuple[str, str]]], actual: dict[str, set[tuple[str, str]]]
) -> list[str]:
    """Differences between the nets the schematic should have and the ones KiCad sees."""
    problems: list[str] = []
    wanted = {frozenset(nodes): net for net, nodes in expected.items()}
    seen = {frozenset(nodes): net for net, nodes in actual.items()}
    for nodes, net in wanted.items():
        if nodes not in seen:
            problems.append(f"net {net}: expected {sorted(nodes)}")
    for nodes, net in seen.items():
        if nodes not in wanted:
            problems.append(f"KiCad net {net}: {sorted(nodes)} is not in the design")
    return problems


# --- export ------------------------------------------------------------------------------

# Concern KiCad's library tables, which a command-line run does not have.
_IGNORED_ERC = frozenset({"lib_symbol_issues", "footprint_link_issues", "lib_symbol_mismatch"})


def _same_symbol(texts: list[str]) -> bool:
    """Both pins of a pin conflict belong to one symbol (e.g. a module's GND and AGND
    outputs tied together by its own pinout): a property of the part, not the design."""
    refs = {m[1] for t in texts if (m := re.match(r"Symbol (\S+) Pin", t))}
    return len(refs) == 1 and len(texts) > 1


def export(design: Design, diag: Diagnostics) -> dict[str, JsonValue]:
    """Write out/kicad/<name>.kicad_sch; with kicad-cli, check its netlist against the
    design, run KiCad's ERC (as warnings), and plot it."""
    paths = design.paths
    out = paths.out_dir / "kicad"
    out.mkdir(parents=True, exist_ok=True)
    try:
        schematic = build(design)
    except SymbolError as exc:
        diag.warning("kicad_sch_skipped", f"KiCad schematic not written: {exc}")
        return {}
    sch = out / f"{design.project.name}.kicad_sch"
    sch.write_text(schematic.text, encoding="utf-8")
    data: dict[str, JsonValue] = {
        "schematic": paths.rel(sch),
        "paper": schematic.paper,
        "symbols": dict(schematic.symbols),
    }
    try:
        kicad.cli()
    except kicad.KicadError:
        return data  # kicad-cli is optional here: the file is still useful
    try:
        nets = netlist_nets(kicad.sch_netlist(sch, out / f"{design.project.name}.net"))
        problems = compare(schematic.expected, nets)
        if problems:
            diag.fail_tool(
                "kicad_sch_mismatch",
                "the generated schematic does not connect like the design: "
                + "; ".join(problems[:5]),
            )
        data["netlist_matches"] = not problems
        report = kicad.sch_erc(sch, out / "erc.json")
        counts = {"errors": 0, "warnings": 0}
        for sheet in report.get("sheets", []):
            for item in sheet.get("violations", []):
                rule = str(item.get("type", "unknown"))
                texts = [str(i.get("description", "")) for i in item.get("items", [])]
                if rule in _IGNORED_ERC or (rule == "pin_to_pin" and _same_symbol(texts)):
                    continue
                counts["errors" if item.get("severity") == "error" else "warnings"] += 1
                diag.warning(
                    f"kicad_erc_{rule}",
                    f"KiCad ERC {rule}: {item.get('description', '')}"
                    + (" — " + "; ".join(texts) if texts else ""),
                )
        data["erc"] = {"report": paths.rel(out / "erc.json"), **counts}
        svg = kicad.sch_svg(sch, out)
        data["svg"] = paths.rel(svg)
        if rasterize.available() is not None:
            png = svg.with_suffix(".png")
            rasterize.svg_to_png(svg, png, paths.out_dir / ".chrome-profile")
            data["png"] = paths.rel(png)
    except (kicad.KicadError, OSError, RuntimeError, ValueError) as exc:
        diag.fail_tool("kicad_sch", f"KiCad schematic check failed: {exc}")
    return data
