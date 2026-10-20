"""KiCad schematic (`.kicad_sch`) export: the circuit as a KiCad 10 schematic.

Symbols come from the KiCad libraries (`kicad:` in each part, with fallbacks); parts
without one get a generated box symbol. Parts on signal nets are placed next to the parts
they connect to and joined by routed wires; ground and supply rails get power symbols,
unused pins get no-connect flags, and supplies sit in a row above the circuit. A pin the
router cannot reach is joined by a net label instead.

The output is checked by re-exporting the netlist with kicad-cli (`eda render`).
"""

import copy
import heapq
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
STUB = 2.54  # wire from a pin to its power symbol
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
    unrouted: list[str] = field(default_factory=lambda: list[str]())  # nets joined by labels
    drawing: Box = (0.0, 0.0, 0.0, 0.0)  # the area drawn on, sheet millimetres
    references: dict[str, str] = field(default_factory=lambda: dict[str, str]())  # renamed


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


# --- geometry ----------------------------------------------------------------------------

type Point = tuple[float, float]
type Dir = tuple[int, int]
type Cell = tuple[int, int]

CELL = GRID / 2  # routing grid: KiCad's 1.27 mm connection grid
MARGIN = 2 * GRID  # free space kept around each part (its fields included) for wires
GAP = 1.27  # between a body and its fields
_SIDES: tuple[Dir, ...] = ((1, 0), (-1, 0), (0, -1), (0, 1))  # right, left, up, down


def _turn(dx: float, dy: float, rotation: int, mirror: bool = False) -> Point:
    """A library offset (y up) on the sheet (y down), for a symbol KiCad turned by
    `rotation` degrees (counter-clockwise), mirrored left-right first if `mirror`."""
    x, y = (-dx if mirror else dx), -dy
    c, s = round(math.cos(math.radians(rotation))), round(math.sin(math.radians(rotation)))
    return (x * c + y * s, -x * s + y * c)


def _outward(angle: float, rotation: int, mirror: bool = False) -> Dir:
    """The direction from a pin's connection point away from the body, on the sheet."""
    rad = math.radians(angle)
    dx, dy = _turn(-math.cos(rad), -math.sin(rad), rotation, mirror)
    return (round(dx), round(dy))


def _turn_box(box: Box, rotation: int, x: float, y: float, mirror: bool = False) -> Box:
    corners = [
        _turn(cx, cy, rotation, mirror) for cx in (box[0], box[2]) for cy in (box[1], box[3])
    ]
    return (
        x + min(c[0] for c in corners),
        y + min(c[1] for c in corners),
        x + max(c[0] for c in corners),
        y + max(c[1] for c in corners),
    )


def _union(boxes: list[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _grow(box: Box, by: float) -> Box:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _snap(value: float) -> float:
    return round(value / GRID) * GRID


def _mm(value: float) -> str:
    text = f"{round(value, 4):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _text_width(text: str) -> float:
    return len(text) * TEXT * 0.8


def _label_width(text: str) -> float:
    return _text_width(text) + 2.0


@dataclass(frozen=True)
class _Text:
    """A symbol field: position, the angle written to the file (KiCad adds the symbol's
    rotation), and the area it covers on the sheet."""

    name: str
    value: str
    x: float
    y: float
    box: Box
    angle: float = 0.0
    justify: str | None = None
    hide: bool = False


def _centred(name: str, value: str, x: float, y: float, rotation: int) -> _Text:
    """A horizontal field centred on (x, y), whatever the symbol's rotation."""
    w = _text_width(value) / 2
    box = (x - w, y - TEXT / 2, x + w, y + TEXT / 2)
    return _Text(name, value, x, y, box, 90.0 if rotation in (90, 270) else 0.0)


def _library_field(
    sym: LibSymbol, name: str, value: str, x: float, y: float, rotation: int, hide: bool = False
) -> _Text:
    """A field where the library symbol puts it (designers place these to clear the pins),
    turned with the symbol (0° or 180°)."""
    found = next((p for p in children(sym.tree, "property") if p[1] == name), None)
    at = child(found, "at") if found is not None else None
    px, py = (number(at[1]), number(at[2])) if at is not None else (0.0, 0.0)
    if rotation == 180:
        px, py = -px, -py
    justify: str | None = None
    effects = child(found, "effects") if found is not None else None
    just = child(effects, "justify") if effects is not None else None
    words: list[str] = []
    if just is not None:
        words = [str(w) for w in just[1:]]
        if rotation == 180:
            flip = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
            words = [flip.get(w, w) for w in words]
        justify = " ".join(words)
    angle = number(at[3]) if at is not None and len(at) > 3 else 0.0
    fx, fy = x + px, y - py
    w = _text_width(value)
    if angle % 180:  # vertical text
        box = (fx - TEXT, fy - w, fx + TEXT, fy + w)
    elif "left" in words:
        box = (fx, fy - TEXT, fx + w, fy + TEXT)
    elif "right" in words:
        box = (fx - w, fy - TEXT, fx, fy + TEXT)
    else:
        box = (fx - w / 2, fy - TEXT, fx + w / 2, fy + TEXT)
    return _Text(name, value, fx, fy, box, angle, justify, hide)


@dataclass(frozen=True)
class _Power:
    """A ground or rail symbol hung on a pin by a short wire."""

    path: tuple[Point, ...]
    lib_id: str
    value: str
    x: float
    y: float
    rotation: int
    box: Box  # wire, symbol and value


def _power(net: str, sign: int, point: Point, d: Dir) -> _Power:
    """The wire runs outward, turning down (ground, negative rails) or up (positive rails)
    when the pin points sideways, so the symbols stand the usual way up."""
    px, py = point
    ex, ey = px + d[0] * STUB, py + d[1] * STUB
    down = net == GROUND or sign < 0
    path = [(px, py), (ex, ey)]
    if d[1] == 0:
        ey += STUB if down else -STUB
        path.append((ex, ey))
        facing = 0 if down else 180
    else:
        facing = 0 if d[1] > 0 else 180
    if net == GROUND:
        lib_id, rotation = "power:GND", facing
    else:  # power symbols point up at rotation 0, ground symbols down
        lib_id, rotation = ("power:VCC" if sign > 0 else "power:VEE"), 180 - facing
    sym = load_symbol(lib_id)
    value = _library_field(sym, "Value", net, ex, ey, rotation)
    xs, ys = [q[0] for q in path], [q[1] for q in path]
    box = _union(
        [(min(xs), min(ys), max(xs), max(ys)), _turn_box(sym.box, rotation, ex, ey), value.box]
    )
    return _Power(tuple(path), lib_id, net, ex, ey, rotation, box)


@dataclass
class _Item:
    """A part on the sheet."""

    part: ResolvedPart
    sym: LibSymbol
    mapping: dict[str, list[str]]  # part pin → symbol pin numbers
    nets: dict[str, str]  # symbol pin number → net
    power: dict[str, int] = field(default_factory=lambda: dict[str, int]())  # net → sign
    ref: str = ""  # reference in KiCad (numbered), set by `build`
    rotation: int = 0
    mirror: bool = False  # left-right, only with rotation 0
    x: float = 0.0
    y: float = 0.0

    def pins(self) -> list[tuple[Point, Dir, str | None]]:
        """Connection points with their outward direction and net; stacked pins (e.g. a
        module's GND pins) share one point."""
        found: dict[tuple[float, float], tuple[Point, Dir, str | None]] = {}
        for pin in self.sym.pins:
            dx, dy = _turn(pin.x, pin.y, self.rotation, self.mirror)
            point = (self.x + dx, self.y + dy)
            key = (round(point[0], 3), round(point[1], 3))
            net = self.nets.get(pin.number)
            if key not in found or (found[key][2] is None and net is not None):
                found[key] = (point, _outward(pin.angle, self.rotation, self.mirror), net)
        return list(found.values())

    def body(self) -> Box:
        return _turn_box(self.sym.box, self.rotation, self.x, self.y, self.mirror)

    def fields(self) -> list[_Text]:
        ref, value = self.ref, self.part.label() or self.part.spec.id
        if self.rotation in (0, 180) and not self.mirror and len(self.sym.pins) > 3:
            return [
                _library_field(self.sym, "Reference", ref, self.x, self.y, self.rotation),
                _library_field(self.sym, "Value", value, self.x, self.y, self.rotation),
            ]
        x0, y0, x1, y1 = self.body()
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        pinned = {d for _, d, _ in self.pins()}
        dirs = set(pinned)
        for _, d, net in self.pins():  # a sideways pin's power symbol turns up or down
            if net is not None and net in self.power and d[1] == 0:
                dirs.add((0, 1) if net == GROUND or self.power[net] < 0 else (0, -1))
        r = self.rotation
        if dirs <= {(1, 0), (-1, 0)}:  # pins left and right: reference above, value below
            return [
                _centred("Reference", ref, cx, y0 - GAP - TEXT / 2, r),
                _centred("Value", value, cx, y1 + GAP + TEXT / 2, r),
            ]
        side = next(
            (d for d in _SIDES if d not in dirs),
            next((d for d in _SIDES if d not in pinned), _SIDES[0]),
        )
        texts: list[_Text] = []
        for row, (name, text) in enumerate((("Reference", ref), ("Value", value))):
            half = _text_width(text) / 2
            if side == (1, 0):
                texts.append(_centred(name, text, x1 + GAP + half, cy + (row - 0.5) * GRID, r))
            elif side == (-1, 0):
                texts.append(_centred(name, text, x0 - GAP - half, cy + (row - 0.5) * GRID, r))
            elif side == (0, -1):
                texts.append(_centred(name, text, cx, y0 - GAP - TEXT / 2 - (1 - row) * GRID, r))
            else:
                texts.append(_centred(name, text, cx, y1 + GAP + TEXT / 2 + row * GRID, r))
        return texts

    def powers(self) -> list[tuple[str, _Power]]:
        """Ground and rail symbols on the pins, with the key of the pin."""
        return [
            (
                f"{self.part.ref}:{_mm(point[0])}:{_mm(point[1])}",
                _power(net, self.power[net], point, d),
            )
            for point, d, net in self.pins()
            if net is not None and net in self.power
        ]

    def extent(self) -> Box:
        return _union(
            [self.body(), *(t.box for t in self.fields()), *(p.box for _, p in self.powers())]
        )


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
    name: str,
    value: str,
    x: float,
    y: float,
    hide: bool,
    justify: str | None = None,
    angle: float = 0.0,
) -> list[SExpr]:
    return [
        Atom("property"),
        name,
        value,
        [Atom("at"), Atom(_mm(x)), Atom(_mm(y)), Atom(_mm(angle))],
        _effects(justify, hide),
    ]


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
        footprint: str,
        x: float,
        y: float,
        rotation: int,
        fields: list[_Text],
        power: bool = False,
        mirror: bool = False,
    ) -> None:
        self.lib.setdefault(sym.lib_id, sym.tree)
        node: list[SExpr] = [
            Atom("symbol"),
            [Atom("lib_id"), sym.lib_id],
            [Atom("at"), Atom(_mm(x)), Atom(_mm(y)), Atom(str(rotation))],
            *([[Atom("mirror"), Atom("y")]] if mirror else []),
            [Atom("unit"), Atom("1")],
            [Atom("exclude_from_sim"), Atom("no")],
            [Atom("in_bom"), Atom("no" if power else "yes")],
            [Atom("on_board"), Atom("no" if power else "yes")],
            [Atom("dnp"), Atom("no")],
            [Atom("uuid"), self._uuid(f"symbol:{ref}")],
            *(_property(t.name, t.value, t.x, t.y, t.hide, t.justify, t.angle) for t in fields),
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

    def power_symbol(self, lib_id: str, value: str, x: float, y: float, rotation: int) -> None:
        """A power symbol: GND, a supply rail, or PWR_FLAG."""
        self.power += 1
        ref = f"#{'FLG' if lib_id == 'power:PWR_FLAG' else 'PWR'}{self.power:02d}"
        sym = load_symbol(lib_id)
        fields = [
            _library_field(sym, "Reference", ref, x, y, rotation, hide=True),
            _library_field(sym, "Value", value, x, y, rotation),
        ]
        self.symbol(sym, ref, "", x, y, rotation, fields, power=True)

    def wire(self, points: list[Point], key: str) -> None:
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

    def junction(self, x: float, y: float) -> None:
        self.items.append(
            [
                Atom("junction"),
                [Atom("at"), Atom(_mm(x)), Atom(_mm(y))],
                [Atom("diameter"), Atom("0")],
                [Atom("color"), Atom("0"), Atom("0"), Atom("0"), Atom("0")],
                [Atom("uuid"), self._uuid(f"junction:{_mm(x)}:{_mm(y)}")],
            ]
        )

    def label(self, net: str, x: float, y: float, outward: Dir, key: str) -> None:
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


# label angle and justification for each outward direction
_LABEL: dict[Dir, tuple[int, str]] = {
    (1, 0): (0, "left bottom"),
    (-1, 0): (180, "right bottom"),
    (0, -1): (90, "left bottom"),
    (0, 1): (270, "right bottom"),
}


# --- nets --------------------------------------------------------------------------------


def _rails(items: list[_Item]) -> dict[str, int]:
    """Supply rails, drawn with power symbols: nets a DC source holds against ground (+1
    above it, -1 below) and nets on a symbol's power pins."""
    rails: dict[str, int] = {}
    for item in items:
        kinds = {pin.number: pin.kind for pin in item.sym.pins}
        for num, net in item.nets.items():
            if net != GROUND and kinds.get(num) in ("power_in", "power_out"):
                rails.setdefault(net, 1)
    for item in items:
        if item.part.spec.id != "dc_voltage_source":
            continue
        circuit_net = {pin: item.nets.get(nums[0]) for pin, nums in item.mapping.items()}
        plus, minus = circuit_net.get("+"), circuit_net.get("-")
        if minus == GROUND and plus not in (None, GROUND):
            rails[str(plus)] = 1
        elif plus == GROUND and minus not in (None, GROUND):
            rails[str(minus)] = -1
    return rails


def _flagged(items: list[_Item], rails: dict[str, int]) -> list[str]:
    """Power nets that need a PWR_FLAG: KiCad's ERC wants every net with power inputs
    (power symbols are some) driven, and no symbol pin on them is a power output."""
    used = {net for item in items for net in item.nets.values()}
    power = [net for net in used if net == GROUND or net in rails]
    driven = {
        net
        for item in items
        for pin in item.sym.pins
        if pin.kind == "power_out" and (net := item.nets.get(pin.number)) is not None
    }
    return sorted((n for n in power if n not in driven), key=lambda n: (n != GROUND, n))


# --- placement ---------------------------------------------------------------------------


def _orientations(item: _Item) -> list[tuple[int, bool]]:
    """Rotations and mirroring tried for a part: sources stand upright, ICs keep their
    library orientation, connectors may face either way."""
    if item.part.spec.category == "source":
        return [(0, False)]
    if len(item.sym.pins) > 3:
        return [(0, False), (0, True)] if item.part.spec.category == "connector" else [(0, False)]
    return [(0, False), (90, False), (180, False), (270, False), (0, True)]


def _link(p: Point, d: Dir, t: Point, td: Dir) -> float:
    """Estimated cost of a wire between two pins: its length, a bend, and detours when a
    pin points away from the other."""
    vx, vy = t[0] - p[0], t[1] - p[1]
    cost = abs(vx) + abs(vy)
    if abs(vx) > 1e-6 and abs(vy) > 1e-6:
        cost += GRID
    if d[0] * vx + d[1] * vy <= 1e-6:
        cost += 4 * GRID
    if -(td[0] * vx + td[1] * vy) <= 1e-6:
        cost += 4 * GRID
    return cost


def _upside_down(item: _Item) -> float:
    """Penalty for ground and negative rails hung on upward pins (and positive rails on
    downward ones): their symbols would be drawn upside down."""
    cost = 0.0
    for _, d, net in item.pins():
        if net is not None and net in item.power:
            sign = item.power[net]
            if (d == (0, -1) and sign < 0) or (d == (0, 1) and sign > 0):
                cost += 4 * GRID
    return cost


def _targets(items: list[_Item], signal: set[str]) -> dict[str, list[tuple[Point, Dir]]]:
    targets: dict[str, list[tuple[Point, Dir]]] = {}
    for item in items:
        for point, d, net in item.pins():
            if net is not None and net in signal:
                targets.setdefault(net, []).append((point, d))
    return targets


def _first(items: list[_Item], signal: set[str]) -> _Item | None:
    """The part placement starts from: the one on the most signal nets."""
    cluster = [i for i in items if any(net in signal for net in i.nets.values())]
    if not cluster:
        return None
    return max(
        cluster,
        key=lambda i: (len({n for n in i.nets.values() if n in signal}), -items.index(i)),
    )


def _priority(
    item: _Item, items: list[_Item], signal: set[str], targets: dict[str, list[tuple[Point, Dir]]]
) -> tuple[int, int, float, float, int]:
    """Which part to place next: the best connected; among equals, the one wired to the
    top-most (then left-most) placed pin, so parts fanning out of an IC keep its pin order."""
    nets = {n for n in item.nets.values() if n in signal}
    reached = [p for n in nets if n in targets for p, _ in targets[n]]
    top = min(reached, key=lambda p: (p[1], p[0]), default=(0.0, 0.0))
    return (len(nets & targets.keys()), len(nets), -top[1], -top[0], -items.index(item))


HUB_PINS = 8  # a first part with this many pins (an IC, a module) gets rows fanned out


@dataclass
class _Fanout:
    """A wire planned from a side pin of an IC to the first part of that pin's row: out
    from the pin, turning `turn` millimetres away (clear of power symbols on the IC's
    pins) toward the row, then into the part. Higher rows turn farther out, so the wires
    of one side never cross."""

    net: str
    hub: _Item
    side: Dir
    part: _Item
    turn: float

    def points(self) -> list[Point]:
        p = next(p for p, d, n in self.hub.pins() if n == self.net and d == self.side)
        q = next(p for p, _, n in self.part.pins() if n == self.net)
        if abs(p[1] - q[1]) < 1e-6:
            return [p, q]
        tx = p[0] + self.side[0] * self.turn
        return [p, (tx, p[1]), (tx, q[1]), q]


def _facing(item: _Item, net: str, toward: Dir, hub: _Item) -> Point | None:
    """Turn `item` so its pin on `net` faces `toward` (the IC), preferring no power
    symbols between it and the IC, its other pins to the IC in the IC's pin order, and
    symbols the right way up. Returns that pin relative to the item's origin, or None if
    no orientation makes it face the IC."""
    order = {n: p[1] for p, _, n in hub.pins() if n is not None}
    best: tuple[float, int, bool, Point] | None = None
    for rotation, mirror in _orientations(item):
        item.rotation, item.mirror, item.x, item.y = rotation, mirror, 0.0, 0.0
        pins = item.pins()
        pin = next((p for p, d, n in pins if n == net and d == toward), None)
        if pin is None:
            continue
        cost = _upside_down(item)
        cost += 10 * GRID * sum(d == toward and n in item.power for _, d, n in pins)
        shared = sorted((p[1], order[n]) for p, d, n in pins if d == toward and n in order)
        cost += 10 * GRID * sum(b[1] < a[1] for a, b in pairwise(shared))
        if best is None or cost < best[0]:
            best = (cost, rotation, mirror, pin)
    if best is None:
        return None
    _, item.rotation, item.mirror, pin = best
    return pin


def _fan_out(hub: _Item, items: list[_Item], signal: set[str]) -> tuple[list[_Item], list[_Fanout]]:
    """Rows of parts beside an IC: each signal pin on its left and right sides starts a
    row with the part it connects to, followed by the chain of two-pin parts after it
    (e.g. GPIO → resistor → LED → ground). Rows keep the pins' order top to bottom."""
    placed: list[_Item] = []
    plans: list[_Fanout] = []
    for side in ((-1, 0), (1, 0)):
        starts: list[tuple[Point, str, _Item]] = []
        taken = {id(i) for i in placed}
        for point, d, net in sorted(hub.pins(), key=lambda t: (t[0][1], t[0][0])):
            if d != side or net is None or net not in signal:
                continue
            members = [i for i in items if i is not hub and net in i.nets.values()]
            if len(members) == 1 and id(members[0]) not in taken:
                starts.append((point, net, members[0]))
                taken.add(id(members[0]))
        if not starts:
            continue
        rows = len(starts)
        edge = starts[0][0][0] + side[0] * ((rows + 3) * GRID + MARGIN)
        bottom = -math.inf
        for k, (point, net, part) in enumerate(starts):
            cursor, row_y, entry, chain = edge, point[1], net, part
            row: list[_Item] = []
            while True:
                found = _facing(chain, entry, (-side[0], -side[1]), hub)
                if found is None:
                    break
                py = found[1]
                ext = chain.extent()
                if side[0] < 0:
                    x = math.floor((cursor - ext[2]) / GRID) * GRID
                else:
                    x = math.ceil((cursor - ext[0]) / GRID) * GRID
                y = _snap(row_y - py)
                if not row and ext[1] + y < bottom + GRID:  # below the row above
                    y = math.ceil((bottom + GRID - ext[1]) / GRID) * GRID
                    row_y = y + py
                chain.x, chain.y = x, y
                row.append(chain)
                ext = chain.extent()
                cursor = ext[0] - MARGIN if side[0] < 0 else ext[2] + MARGIN
                # Follow the chain through a two-pin part to the next one.
                after = [n for n in set(chain.nets.values()) if n in signal and n != entry]
                if len(chain.sym.pins) != 2 or len(after) != 1:
                    break
                entry = after[0]
                nxt = [i for i in items if i is not chain and entry in i.nets.values()]
                if len(nxt) != 1 or nxt[0] is hub or id(nxt[0]) in taken | {id(r) for r in row}:
                    break
                chain = nxt[0]
            if not row:
                continue
            placed += row
            taken |= {id(r) for r in row}
            bottom = max(bottom, *(r.extent()[3] for r in row))
            plans.append(_Fanout(net, hub, side, row[0], (rows - k + 2) * GRID))
    return placed, plans


def _place(
    items: list[_Item], signal: set[str], first: tuple[int, bool], refine: bool
) -> tuple[list[_Item], list[_Fanout]]:
    """Place the parts on signal nets next to the parts they connect to, greedily, best
    connected first (the first one turned to `first`; an IC first gets rows fanned out of
    its sides); with `refine`, then move each greedily placed part to its best spot with
    the others fixed until nothing improves. Returns them in placement order, and the
    planned fan-out wires."""
    cluster = [i for i in items if any(net in signal for net in i.nets.values())]
    placed: list[_Item] = []
    plans: list[_Fanout] = []
    start = _first(items, signal)
    if start is not None:
        start.rotation, start.mirror = first
        placed.append(start)
        if len(start.sym.pins) >= HUB_PINS:
            rows, plans = _fan_out(start, items, signal)
            placed += rows
    fixed = len(placed)
    while len(placed) < len(cluster):
        targets = _targets(placed, signal)
        rest = [i for i in cluster if i not in placed]
        item = max(rest, key=lambda i: _priority(i, items, signal, targets))
        _spot(item, placed, targets)
        placed.append(item)
    for _ in range(3 if refine else 0):
        moved = False
        for item in placed[fixed:]:
            others = [i for i in placed if i is not item]
            if _spot(item, others, _targets(others, signal), keep=True):
                moved = True
        if not moved:
            break
    return placed, plans


def _crosses(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Whether segments a-b and c-d cross (touching ends do not count)."""

    def side(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = side(c, d, a), side(c, d, b)
    d3, d4 = side(a, b, c), side(a, b, d)
    return d1 * d2 < -1e-9 and d3 * d4 < -1e-9


def _sketch(targets: dict[str, list[tuple[Point, Dir]]]) -> list[tuple[Point, Point]]:
    """Straight-line stand-ins for the wires between placed pins: each pin to the nearest
    earlier pin of its net."""
    lines: list[tuple[Point, Point]] = []
    for pins in targets.values():
        points = [p for p, _ in pins]
        for i, p in enumerate(points[1:], start=1):
            q = min(points[:i], key=lambda t: abs(t[0] - p[0]) + abs(t[1] - p[1]))
            lines.append((p, q))
    return lines


CROSSING = 3 * GRID  # placement penalty per estimated wire crossing
type Links = list[tuple[Point, Point]]  # pin → the pin it is wired to, as straight lines
SHORTLIST = 40  # candidate spots checked for crossings


def _spot(
    item: _Item,
    others: list[_Item],
    targets: dict[str, list[tuple[Point, Dir]]],
    keep: bool = False,
) -> bool:
    """Move `item` to the free spot (on the 2.54 mm grid) with the cheapest connections
    (length, bends, detours, then crossings among the best few); with `keep`, only if that
    beats where it is. Returns whether it moved."""
    boxes = [p.extent() for p in others]
    region = _union(boxes)
    centre = ((region[0] + region[2]) / 2, (region[1] + region[3]) / 2)
    sketch = _sketch(targets)

    def cost(
        pins: list[tuple[Point, Dir, list[tuple[Point, Dir]]]], box: Box
    ) -> tuple[float, Links]:
        total, links = 0.0, list[tuple[Point, Point]]()
        for p, d, ts in pins:
            value, t = min((_link(p, d, t, td), t) for t, td in ts)
            total += value
            links.append((p, t))
        mid = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        return total + 0.05 * (abs(mid[0] - centre[0]) + abs(mid[1] - centre[1])), links

    def crossings(links: Links) -> float:
        return CROSSING * sum(_crosses(p, t, a, b) for p, t in links for a, b in sketch)

    def linked() -> list[tuple[Point, Dir, list[tuple[Point, Dir]]]]:
        return [(p, d, targets[n]) for p, d, n in item.pins() if n is not None and n in targets]

    start = (item.rotation, item.mirror, item.x, item.y)
    current: tuple[float, int, bool, float, float] | None = None
    if keep:
        value, links = cost(linked(), _grow(item.extent(), MARGIN))
        current = (value + _upside_down(item) + crossings(links), *start)
    found: list[tuple[float, int, int, bool, float, float, Links]] = []
    for rotation, mirror in _orientations(item):
        item.rotation, item.mirror, item.x, item.y = rotation, mirror, 0.0, 0.0
        ext = _grow(item.extent(), MARGIN)
        pins = linked()
        flipped = _upside_down(item)
        reach = 4 * GRID
        xs = range(
            round((region[0] - ext[2] - reach) / GRID),
            round((region[2] - ext[0] + reach) / GRID) + 1,
        )
        ys = range(
            round((region[1] - ext[3] - reach) / GRID),
            round((region[3] - ext[1] + reach) / GRID) + 1,
        )
        for gx in xs:
            x = gx * GRID
            for gy in ys:
                y = gy * GRID
                box = (ext[0] + x, ext[1] + y, ext[2] + x, ext[3] + y)
                if any(_overlaps(box, b) for b in boxes):
                    continue
                moved = [((p[0] + x, p[1] + y), d, ts) for p, d, ts in pins]
                value, links = cost(moved, box)
                found.append((value + flipped, len(found), rotation, mirror, x, y, links))
    assert found  # the search area always reaches past the other parts
    found.sort(key=lambda f: (f[0], f[1]))
    best = current
    for value, _, rotation, mirror, x, y, links in found[:SHORTLIST]:
        if best is not None and value >= best[0] - 1e-6:
            break  # sorted: no later spot can beat it
        total = value + crossings(links)
        if best is None or total < best[0] - 1e-6:
            best = (total, rotation, mirror, x, y)
    assert best is not None
    _, item.rotation, item.mirror, item.x, item.y = best
    return (item.rotation, item.mirror, item.x, item.y) != start


# --- routing -----------------------------------------------------------------------------


def _cell(p: Point) -> Cell:
    return (round(p[0] / CELL), round(p[1] / CELL))


def _on_grid(p: Point) -> bool:
    return all(abs(v / CELL - round(v / CELL)) < 1e-3 for v in p)


def _steps(corners: list[Cell]) -> list[Cell]:
    """Every grid cell along a path of straight runs between `corners`."""
    path = [corners[0]]
    for a, b in pairwise(corners):
        dx, dy = (b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1])
        c = a
        while c != b:
            c = (c[0] + dx, c[1] + dy)
            path.append(c)
    return path


def _cells(box: Box) -> list[Cell]:
    """Grid cells inside a box, edges included."""
    x0, y0 = math.ceil(box[0] / CELL - 1e-3), math.ceil(box[1] / CELL - 1e-3)
    x1, y1 = math.floor(box[2] / CELL + 1e-3), math.floor(box[3] / CELL + 1e-3)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def _axis(d: Dir) -> str:
    return "h" if d[0] else "v"


class _Router:
    """Orthogonal maze routing on the 1.27 mm grid. Wires of different nets may cross at
    right angles but never overlap, bend or end on each other, or touch a pin of another
    net, so KiCad connects exactly what the design connects."""

    BEND = 3.0
    CROSS = 4.0
    TEXT_COST = 3.0
    LIMIT = 200_000

    def __init__(self, bounds: tuple[int, int, int, int]) -> None:
        self.bounds = bounds
        self.blocked: set[Cell] = set()
        self.soft: dict[Cell, float] = {}
        self.reserved: dict[Cell, str] = {}  # the first step out of a pin
        self.nodes: dict[Cell, str] = {}  # pins, wire ends, bends, junctions
        self.axes: dict[Cell, dict[str, set[str]]] = {}
        self.edges: dict[str, set[tuple[Cell, Cell]]] = {}
        self.terminals: dict[Cell, str] = {}
        self.conflicts: set[Cell] = set()
        self.total = 0.0  # cost of the committed wires

    def reserve(self, cell: Cell, net: str) -> None:
        if self.reserved.get(cell, net) != net:
            self.conflicts.add(cell)  # two pins face each other closely: neither may use it
        self.reserved[cell] = net

    def open_pins(self) -> None:
        """Pins and their first step out are free for their own net only."""
        self.blocked -= self.terminals.keys() | self.reserved.keys()
        self.blocked |= self.conflicts

    def _others(self, cell: Cell, net: str) -> set[str]:
        """Axes of other nets' wires through a cell."""
        return {a for n, axes in self.axes.get(cell, {}).items() if n != net for a in axes}

    def route(
        self, net: str, start: Cell, out: Dir, goals: dict[Cell, Dir | None]
    ) -> list[Cell] | None:
        """The cheapest path from a pin (leaving it outward) to any goal cell; a goal with
        a direction must be entered moving that way (into a pin)."""
        if not goals:
            return None
        gx0 = min(c[0] for c in goals)
        gx1 = max(c[0] for c in goals)
        gy0 = min(c[1] for c in goals)
        gy1 = max(c[1] for c in goals)

        def h(c: Cell) -> float:
            return max(gx0 - c[0], 0, c[0] - gx1) + max(gy0 - c[1], 0, c[1] - gy1)

        x0, y0, x1, y1 = self.bounds
        best: dict[tuple[Cell, Dir], float] = {(start, out): 0.0}
        came: dict[tuple[Cell, Dir], tuple[Cell, Dir] | None] = {(start, out): None}
        queue: list[tuple[float, int, float, Cell, Dir]] = [(h(start), 0, 0.0, start, out)]
        count = 0
        while queue:
            _, _, g, cell, d = heapq.heappop(queue)
            if g > best.get((cell, d), math.inf):
                continue
            if cell != start and cell in goals:
                self.total += g
                path: list[Cell] = []
                state: tuple[Cell, Dir] | None = (cell, d)
                while state is not None:
                    path.append(state[0])
                    state = came[state]
                return path[::-1]
            count += 1
            if count > self.LIMIT:
                return None
            if cell == start or self._others(cell, net):
                moves = [d]  # leave a pin outward; go straight across another wire
            else:
                moves = [m for m in _SIDES if m != (-d[0], -d[1])]
            for m in moves:
                n = (cell[0] + m[0], cell[1] + m[1])
                if not (x0 <= n[0] <= x1 and y0 <= n[1] <= y1):
                    continue
                cost = g + 1.0 + (self.BEND if m != d else 0.0)
                if n in goals:
                    need = goals[n]
                    if need is not None and need != m:
                        continue
                else:
                    if n in self.blocked or n in self.terminals:
                        continue
                    if self.nodes.get(n, net) != net or self.reserved.get(n, net) != net:
                        continue
                    if net in self.axes.get(n, {}):
                        continue  # own wire, but not a usable goal (it crosses another net)
                    others = self._others(n, net)
                    if _axis(m) in others:
                        continue
                    if others:
                        cost += self.CROSS
                    cost += self.soft.get(n, 0.0)
                if cost < best.get((n, m), math.inf):
                    best[(n, m)] = cost
                    came[(n, m)] = (cell, d)
                    heapq.heappush(queue, (cost + h(n), len(came), cost, n, m))
        return None

    def free(self, net: str, path: list[Cell]) -> bool:
        """Whether a planned path can be drawn: it runs between two pins of `net` and
        touches nothing else."""
        if len(path) < 2 or any(self.terminals.get(c) != net for c in (path[0], path[-1])):
            return False
        for c in path[1:-1]:
            if c in self.blocked or c in self.terminals or c in self.nodes or c in self.axes:
                return False
            if self.reserved.get(c, net) != net:
                return False
        return True

    def commit(self, net: str, path: list[Cell]) -> None:
        edges = self.edges.setdefault(net, set())
        for a, b in pairwise(path):
            edges.add((min(a, b), max(a, b)))
            axis = "h" if a[1] == b[1] else "v"
            for c in (a, b):
                self.axes.setdefault(c, {}).setdefault(net, set()).add(axis)
        self.nodes[path[0]] = net
        self.nodes[path[-1]] = net
        for a, b, c in zip(path, path[1:], path[2:], strict=False):
            if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
                self.nodes[b] = net

    def goals(self, net: str, pins: dict[Cell, Dir]) -> dict[Cell, Dir | None]:
        """Where a new branch of `net` may end: its pins (entered from outside) and its
        wires, except where they cross another net."""
        goals: dict[Cell, Dir | None] = {c: (-d[0], -d[1]) for c, d in pins.items()}
        for a, b in self.edges.get(net, set()):
            for c in (a, b):
                if c not in goals and not self._others(c, net):
                    goals[c] = None
        return goals

    def segments(self, net: str) -> tuple[list[tuple[Cell, Cell]], list[Cell]]:
        """The net's wires as straight runs, split at pins, bends and branches; and the
        points where three or more connections meet (junction dots)."""
        edges = self.edges.get(net, set())
        degree: dict[Cell, int] = {}
        for a, b in edges:
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
        for c in degree:
            if self.terminals.get(c) == net:
                degree[c] += 1
        runs: list[tuple[Cell, Cell]] = []
        for horizontal in (True, False):
            steps = sorted(
                (a for a, b in edges if (a[1] == b[1]) == horizontal),
                key=lambda c: (c[1], c[0]) if horizontal else (c[0], c[1]),
            )
            step = (1, 0) if horizontal else (0, 1)
            current: tuple[Cell, Cell] | None = None
            for a in steps:
                b = (a[0] + step[0], a[1] + step[1])
                if current is not None and current[1] == a and not self._breaks(a, degree, step):
                    current = (current[0], b)
                    continue
                if current is not None:
                    runs.append(current)
                current = (a, b)
            if current is not None:
                runs.append(current)
        dots = sorted(c for c, n in degree.items() if n >= 3)
        return runs, dots

    def _breaks(self, cell: Cell, degree: dict[Cell, int], step: Dir) -> bool:
        """A straight run stops at a cell unless the wire just passes through it."""
        return degree.get(cell, 0) != 2 or self.terminals.get(cell) is not None


# --- building ----------------------------------------------------------------------------


def _translate(items: list[_Item], dx: float, dy: float) -> None:
    for item in items:
        item.x += dx
        item.y += dy


def annotate(refs: list[str]) -> dict[str, str]:
    """KiCad references for the design's: KiCad counts a reference without a trailing
    number (`VCC`, `RF`) as not annotated, so those get the first free number (`RF1`)."""
    used = set(refs)
    out: dict[str, str] = {}
    for ref in refs:
        if re.search(r"\d$", ref):
            out[ref] = ref
            continue
        n = 1
        while f"{ref}{n}" in used:
            n += 1
        out[ref] = f"{ref}{n}"
        used.add(out[ref])
    return out


@dataclass
class _Layout:
    """One arrangement of the sheet: part positions, paper, routed wires."""

    positions: list[tuple[int, bool, float, float]]  # per part: rotation, mirror, x, y
    paper: tuple[str, float, float]
    flags: Point  # where the PWR_FLAG row starts
    router: _Router
    labelled: list[tuple[str, Point, Dir, str]]  # net labels on pins the router missed

    def score(self) -> tuple[int, float]:
        return (len(self.labelled), self.router.total)


def _arrange(
    items: list[_Item],
    signal: set[str],
    flagged: list[str],
    first: tuple[int, bool],
    refine: bool,
) -> _Layout:
    """Place the parts, pick the sheet, and route the signal nets."""
    for item in items:
        item.rotation, item.mirror, item.x, item.y = 0, False, 0.0, 0.0
    # Parts on signal nets are placed around each other; the rest (supplies) and the
    # PWR_FLAGs form a row above them.
    cluster, plans = _place(items, signal, first, refine)
    top = _union([i.extent() for i in cluster])[1] if cluster else 0.0
    left = _union([i.extent() for i in cluster])[0] if cluster else 0.0
    x = left
    for item in items:
        if item in cluster:
            continue
        ext = item.extent()
        item.x, item.y = _snap(x - ext[0]), _snap(top - MARGIN - ext[3])
        x = item.x + ext[2] + MARGIN
    step = _flag_step(flagged)
    fx, bottom = _snap(x + GRID), _snap(top - MARGIN)
    boxes = [i.extent() for i in items]
    if flagged:
        boxes.append((fx - step / 2, bottom - 7 * GRID, fx + step * len(flagged), bottom))

    # The smallest sheet that holds it all, with room for wires and the title block.
    content = _union(boxes)
    width, height = content[2] - content[0], content[3] - content[1]
    pad = BORDER + 4 * GRID
    paper = PAPERS[-1]
    for name, w, h in PAPERS:
        if width <= w - 2 * pad and (
            height <= h - 2 * pad - 35.0 or (width <= w - 2 * pad - 115.0 and height <= h - 2 * pad)
        ):
            paper = (name, w, h)
            break
    dx, dy = _snap(pad - content[0]), _snap(pad - content[1])
    _translate(items, dx, dy)

    router = _Router(
        (
            math.ceil(BORDER / CELL),
            math.ceil(BORDER / CELL),
            math.floor((paper[1] - BORDER) / CELL),
            math.floor((paper[2] - BORDER) / CELL),
        )
    )
    terminals: dict[str, dict[Cell, Dir]] = {}
    labelled: list[tuple[str, Point, Dir, str]] = []
    for item in items:
        router.blocked.update(_cells(_grow(item.body(), CELL)))  # keep wires off the body
        for text in item.fields():
            for c in _cells(text.box):
                router.soft[c] = router.soft.get(c, 0.0) + router.TEXT_COST
        for _, power in item.powers():
            router.blocked.update(_cells(_grow(power.box, CELL / 2)))
        for point, d, net in item.pins():
            if net is None or net in item.power:
                continue
            if _on_grid(point):
                cell = _cell(point)
                terminals.setdefault(net, {})[cell] = d
                router.terminals[cell] = net
                router.reserve((cell[0] + d[0], cell[1] + d[1]), net)
            else:
                labelled.append((net, point, d, f"{item.part.ref}:{_mm(point[0])}:{_mm(point[1])}"))
    router.open_pins()

    # Planned fan-out wires first, where nothing is in their way.
    for plan in plans:
        cells = _steps([_cell(p) for p in plan.points()])
        if all(_on_grid(p) for p in plan.points()) and router.free(plan.net, cells):
            router.commit(plan.net, cells)

    # Signal nets, shortest first: each pin joins the wires already drawn for its net.
    def span(net: str) -> int:
        cells = list(terminals[net])
        xs, ys = [c[0] for c in cells], [c[1] for c in cells]
        return max(xs) - min(xs) + max(ys) - min(ys)

    for net in sorted(terminals, key=lambda n: (span(n), n)):
        pins = terminals[net]
        wired = {c for edge in router.edges.get(net, set()) for c in edge}
        order = sorted(pins, key=lambda c: (c not in wired, c[0], c[1]))
        joined: dict[Cell, Dir] = {c: pins[c] for c in order if c in wired} or {
            order[0]: pins[order[0]]
        }
        failed: list[Cell] = []
        for cell in order:
            if cell in joined:
                continue
            path = router.route(net, cell, pins[cell], router.goals(net, joined))
            if path is None:
                failed.append(cell)
                continue
            router.commit(net, path)
            joined[cell] = pins[cell]
        if failed:  # unroutable pins are joined by name instead, labels on the pins
            for cell in [order[0], *failed]:
                point = (cell[0] * CELL, cell[1] * CELL)
                labelled.append((net, point, pins[cell], f"{net}:{cell}"))
    positions = [(i.rotation, i.mirror, i.x, i.y) for i in items]
    return _Layout(positions, paper, (fx + dx, bottom + dy - 3 * GRID), router, labelled)


def _flag_step(flagged: list[str]) -> float:
    """Room for one PWR_FLAG column ("PWR_FLAG" and the net name are centred on it)."""
    return _snap(max([_label_width("PWR_FLAG"), *(_text_width(n) + GRID for n in flagged)]) + GRID)


def build(design: Design) -> Schematic:
    """The schematic of `design.circuit` as `.kicad_sch` text."""
    circuit = design.circuit
    board_footprints = {p.ref: p.footprint for p in design.board.parts} if design.board else {}
    expected: dict[str, set[tuple[str, str]]] = {}
    items: list[_Item] = []
    references = annotate(list(circuit.parts))
    for part in circuit.parts.values():
        sym, mapping = symbol_for(part)
        nets: dict[str, str] = {}
        for pin, numbers in mapping.items():
            net = circuit.net_of(part.ref, pin)
            if net is None:
                continue
            for num in numbers:
                nets[num] = net
                expected.setdefault(net, set()).add((references[part.ref], num))
        items.append(_Item(part, sym, mapping, nets, ref=references[part.ref]))

    rails = _rails(items)
    signal = {net for net in circuit.nets if net != GROUND and net not in rails}
    for item in items:
        item.power = {GROUND: -1, **rails}
    flagged = _flagged(items, rails)

    # Placement is greedy, so try each orientation of the first part, with and without
    # the refining pass, and keep the arrangement that routes best.
    first = _first(items, signal)
    variants = _orientations(first) if first is not None else [(0, False)]
    layout: _Layout | None = None
    for orientation in variants:
        for refine in (True, False):
            candidate = _arrange(items, signal, flagged, orientation, refine)
            if layout is None or candidate.score() < layout.score():
                layout = candidate
    assert layout is not None
    for item, (rotation, mirror, x, y) in zip(items, layout.positions, strict=True):
        item.rotation, item.mirror, item.x, item.y = rotation, mirror, x, y

    writer = _Writer(design)
    symbols: dict[str, str] = {}
    for item in items:
        symbols[item.part.ref] = item.sym.lib_id
        footprint = board_footprints.get(item.part.ref) or (
            item.part.spec.pcb.footprint if item.part.spec.pcb else ""
        )
        writer.symbol(
            item.sym, item.ref, footprint, item.x, item.y, item.rotation, item.fields(),
            mirror=item.mirror,
        )  # fmt: skip
        for point, _, net in item.pins():
            if net is None:
                writer.no_connect(*point, f"{item.part.ref}:{_mm(point[0])}:{_mm(point[1])}")
        for key, power in item.powers():
            writer.power_symbol(power.lib_id, power.value, power.x, power.y, power.rotation)
            writer.wire(list(power.path), key)
    router = layout.router
    for net in sorted(router.edges):
        runs, dots = router.segments(net)
        for i, (a, b) in enumerate(runs):
            writer.wire([(a[0] * CELL, a[1] * CELL), (b[0] * CELL, b[1] * CELL)], f"{net}:{i}")
        for c in dots:
            writer.junction(c[0] * CELL, c[1] * CELL)
    for net, (px, py), d, key in layout.labelled:
        writer.label(net, px, py, d, key)

    # PWR_FLAGs: a rail symbol (or ground) wired to a flag, one per undriven power net.
    fx, fy = layout.flags
    for net in flagged:
        if net == GROUND:
            writer.power_symbol("power:PWR_FLAG", "PWR_FLAG", fx, fy - 2 * GRID, 0)
            writer.wire([(fx, fy - 2 * GRID), (fx, fy)], f"flag:{net}")
            writer.power_symbol("power:GND", "GND", fx, fy, 0)
        else:
            lib = "power:VCC" if rails.get(net, 1) > 0 else "power:VEE"
            writer.power_symbol(lib, net, fx, fy - 2 * GRID, 0)
            writer.wire([(fx, fy - 2 * GRID), (fx, fy)], f"flag:{net}")
            writer.power_symbol("power:PWR_FLAG", "PWR_FLAG", fx, fy, 180)
        fx += _flag_step(flagged)
    unrouted = sorted({net for net, *_ in layout.labelled})
    areas = [i.extent() for i in items]
    if flagged:
        x0, y0 = layout.flags
        areas.append((x0 - GRID * 3, y0 - 5 * GRID, fx, y0 + 2 * GRID))
    for a, b in (e for edges in router.edges.values() for e in edges):
        areas.append((a[0] * CELL, a[1] * CELL, b[0] * CELL, b[1] * CELL))
    paper = layout.paper[0]
    renamed = {ref: kicad_ref for ref, kicad_ref in references.items() if ref != kicad_ref}
    return Schematic(
        writer.document(paper), paper, expected, symbols, unrouted, _union(areas), renamed
    )


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

PX_PER_MM = 12  # the PNG preview's resolution


def crop_svg(svg: Path, out: Path, area: Box, margin: float = 5.0) -> Path:
    """A copy of KiCad's sheet SVG showing only `area` (millimetres, as the SVG's viewBox
    is): the drawing without the empty sheet around it."""
    x0, y0, x1, y1 = _grow(area, margin)
    w, h = x1 - x0, y1 - y0
    text = re.sub(
        r'width="[^"]*" height="[^"]*" viewBox="[^"]*"',
        f'width="{w:.4f}mm" height="{h:.4f}mm" viewBox="{x0:.4f} {y0:.4f} {w:.4f} {h:.4f}"',
        svg.read_text(encoding="utf-8"),
        count=1,
    )
    out.write_text(text, encoding="utf-8")
    return out


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
    if schematic.unrouted:
        data["labelled_nets"] = list(schematic.unrouted)
    if schematic.references:
        data["references"] = dict(schematic.references)
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
        view = crop_svg(svg, out / f"{design.project.name}.view.svg", schematic.drawing)
        data["view"] = paths.rel(view)
        if rasterize.available() is not None:
            png = svg.with_suffix(".png")
            width = min(round((schematic.drawing[2] - schematic.drawing[0] + 10) * PX_PER_MM), 4000)
            rasterize.svg_to_png(view, png, paths.out_dir / ".chrome-profile", width=width)
            data["png"] = paths.rel(png)
    except (kicad.KicadError, OSError, RuntimeError, ValueError) as exc:
        diag.fail_tool("kicad_sch", f"KiCad schematic check failed: {exc}")
    return data
