"""Resolved, checked design objects produced by `eda.design`."""

import re
from dataclasses import dataclass, field, replace

from eda.fab import Fab
from eda.loader import SourceMap
from eda.project import ProjectPaths
from eda.result import Location
from eda.schema.part import PartSpec
from eda.schema.project import Project
from eda.units import format_quantity
from eda.verify.measure import Domain, Measure

GROUND = "GND"

type PinKey = tuple[str, str]  # (part reference, pin name)


_LABEL = re.compile(r"<param:([^>]+)>")


@dataclass(frozen=True)
class ResolvedPart:
    ref: str
    spec: PartSpec
    values: dict[str, float | str]  # SI floats for quantities, strings for choices
    labels: dict[str, str]  # human-readable parameter values
    spice_name: str
    mpn: str | None = None  # manufacturer part number to buy

    def label(self, template: str | None = None) -> str:
        """The symbol label (or `template`) with parameter values filled in, e.g. `330Ω`."""
        text = self.spec.symbol.label if template is None else template
        return _LABEL.sub(lambda m: self.labels.get(m[1], "?"), text)


@dataclass(frozen=True)
class ResolvedCircuit:
    parts: dict[str, ResolvedPart]
    nets: dict[str, tuple[PinKey, ...]]
    source: SourceMap

    def net_of(self, ref: str, pin: str) -> str | None:
        for net, pins in self.nets.items():
            if (ref, pin) in pins:
                return net
        return None


@dataclass(frozen=True)
class Op:
    pass


@dataclass(frozen=True)
class Tran:
    step: float
    stop: float
    start: float | None


@dataclass(frozen=True)
class Ac:
    start: float  # Hz
    stop: float  # Hz
    points_per_decade: int


@dataclass(frozen=True)
class Dc:
    """An operating point at each value of one part parameter (`ref`.`param`)."""

    ref: str
    param: str
    unit: str
    values: tuple[float, ...]  # ascending, start..stop

    @property
    def sweep(self) -> str:
        return f"{self.ref}.{self.param}"


type Analysis = Op | Tran | Ac | Dc


def domain_of(analysis: Analysis) -> Domain:
    if isinstance(analysis, Op):
        return "op"
    if isinstance(analysis, Dc):
        return "dc"
    return "tran" if isinstance(analysis, Tran) else "ac"


def x_unit_of(analysis: Analysis) -> str | None:
    """Unit of the analysis's x axis: time, frequency, or the swept parameter."""
    if isinstance(analysis, Dc):
        return analysis.unit
    return {"tran": "s", "ac": "Hz"}.get(domain_of(analysis))


@dataclass(frozen=True)
class CompiledExpectation:
    index: int
    measure: Measure
    min: float | None
    max: float | None
    location: Location
    start: float | None = None  # tran: evaluate over [start, end] only (s)
    end: float | None = None

    @property
    def label(self) -> str:
        """The measure, with its time range when it has one."""
        if self.start is None and self.end is None:
            return self.measure.text
        start = format_quantity(self.start or 0.0, "s")
        end = format_quantity(self.end, "s") if self.end is not None else "end"
        return f"{self.measure.text} [{start}..{end}]"

    @property
    def range_text(self) -> str:
        unit = self.measure.unit
        low = format_quantity(self.min, unit) if self.min is not None else None
        high = format_quantity(self.max, unit) if self.max is not None else None
        if low and high:
            return f"{low}..{high}"
        return f">= {low}" if low else f"<= {high}"


@dataclass(frozen=True)
class SerialExpectation:
    """A number read from the firmware's UART output (see schema.testspec.Expectation)."""

    index: int
    net: str  # UART TX net
    uart: int  # UART peripheral index
    pattern: str
    min: float | None
    max: float | None
    location: Location

    @property
    def text(self) -> str:
        return f"serial({self.net}) ~ /{self.pattern}/"

    @property
    def range_text(self) -> str:
        if self.min is not None and self.max is not None:
            return f"{self.min:g}..{self.max:g}"
        return f">= {self.min:g}" if self.min is not None else f"<= {self.max:g}"


@dataclass(frozen=True)
class DisplayExpectation:
    """Text on a display part's screen (see schema.testspec.Expectation)."""

    index: int
    ref: str  # display part
    pattern: str | None  # text to find, or
    region: tuple[int, int, int, int] | None  # count lit pixels in x0, y0, x1, y1
    at: float | None  # s; None = end of the run
    min: float | None
    max: float | None
    location: Location

    @property
    def text(self) -> str:
        when = f" @ {format_quantity(self.at, 's')}" if self.at is not None else ""
        if self.region is not None:
            return f"lit({self.ref}, {', '.join(map(str, self.region))}){when}"
        return f"display({self.ref}){when} ~ /{self.pattern}/"

    @property
    def range_text(self) -> str:
        if self.min is None and self.max is None:
            return "match"
        if self.min is not None and self.max is not None:
            return f"{self.min:g}..{self.max:g}"
        return f">= {self.min:g}" if self.min is not None else f"<= {self.max:g}"


@dataclass(frozen=True)
class CompiledEvent:
    """A part parameter change or UART input at `time` during a tran test."""

    time: float  # s
    location: Location
    part: str | None = None
    values: dict[str, tuple[float | str, str]] = field(
        default_factory=lambda: dict[str, tuple[float | str, str]]()
    )  # param → (value, label)
    net: str | None = None  # UART RX net
    uart: int | None = None
    text: str = ""


@dataclass(frozen=True)
class CompiledTest:
    name: str
    description: str
    analysis: Analysis
    expectations: tuple[CompiledExpectation, ...]
    location: Location
    pins: dict[str, str] = field(default_factory=lambda: dict[str, str]())  # net → high/low
    firmware: bool = False  # co-simulate the compiled firmware (tran only)
    serial: tuple[SerialExpectation, ...] = ()
    displays: tuple[DisplayExpectation, ...] = ()
    events: tuple[CompiledEvent, ...] = ()  # sorted by time
    # Per-test part parameters: ref → param → (value, label).
    parts: dict[str, dict[str, tuple[float | str, str]]] = field(
        default_factory=lambda: dict[str, dict[str, tuple[float | str, str]]]()
    )

    def circuit(self, base: "ResolvedCircuit") -> "ResolvedCircuit":
        """The circuit this test simulates: `base` with the test's part parameters."""
        if not self.parts:
            return base
        parts = dict(base.parts)
        for ref, params in self.parts.items():
            part = parts[ref]
            values = {**part.values, **{k: v for k, (v, _) in params.items()}}
            labels = {**part.labels, **{k: label for k, (_, label) in params.items()}}
            parts[ref] = replace(part, values=values, labels=labels)
        return replace(base, parts=parts)

    @property
    def interactive(self) -> bool:
        """Runs through the stepped (co-)simulator instead of one ngspice batch run."""
        return self.firmware or bool(self.events)


@dataclass(frozen=True)
class BoardPlacement:
    x: float  # mm
    y: float  # mm
    rotation: float  # degrees
    side: str  # top | bottom


@dataclass(frozen=True)
class BoardPart:
    ref: str
    value: str
    footprint: str  # KiCad `Library:Name`
    pads: dict[str, str]  # pad number → net
    placement: BoardPlacement | None
    joined_pads: bool = False  # pads with the same number are joined inside the part


@dataclass(frozen=True)
class BoardRules:
    clearance: float  # mm
    track_width: float
    via_diameter: float
    via_drill: float


@dataclass(frozen=True)
class BoardPour:
    net: str
    layers: tuple[str, ...]  # "top" / "bottom"
    clearance: float  # mm
    min_width: float  # mm


@dataclass(frozen=True)
class ResolvedBoard:
    width: float | None  # mm; None = fit to parts
    height: float | None
    margin: float
    spacing: float
    layers: int
    rules: BoardRules
    parts: tuple[BoardPart, ...]
    passes: int
    source: SourceMap
    pours: tuple[BoardPour, ...] = ()
    placer: str = "connected"  # connected | shelf
    fab: Fab | None = None  # PCB house limits, checked when set


@dataclass(frozen=True)
class FirmwareSignal:
    net: str
    pin: str  # MCU part pin, e.g. GP15
    gpio: int  # GPIO number
    mode: str | None  # input | output
    function: str | None  # peripheral function, e.g. UART0_TX
    pull: str  # up | down | none
    initial: str  # high | low
    description: str

    @property
    def macro(self) -> str:
        return f"PIN_{self.net.upper()}"

    def drive(self, override: str | None = None) -> str:
        """Simulated pin behavior: high | low | pullup | pulldown | hiz."""
        if override is not None:
            return override
        if self.mode == "output":
            return self.initial
        if self.function is not None and self.function.endswith(("_TX", "_CSN")):
            return "high"  # UART TX and SPI chip select idle high
        return {"up": "pullup", "down": "pulldown"}.get(self.pull, "hiz")


@dataclass(frozen=True)
class ResolvedFirmware:
    mcu: str  # part reference
    family: str
    signals: dict[str, FirmwareSignal]  # by net
    sources: tuple[str, ...]
    source: SourceMap

    def drives(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """MCU pin → simulated drive, with per-test overrides by net name."""
        overrides = overrides or {}
        return {s.pin: s.drive(overrides.get(s.net)) for s in self.signals.values()}


@dataclass(frozen=True)
class Design:
    paths: ProjectPaths
    project: Project
    circuit: ResolvedCircuit
    tests: dict[str, CompiledTest]
    parts: dict[str, PartSpec]
    board: ResolvedBoard | None = None
    firmware: ResolvedFirmware | None = None
