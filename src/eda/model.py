"""Resolved, checked design objects produced by `eda.design`."""

import re
from dataclasses import dataclass, field, replace

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

    def label(self) -> str:
        """The symbol label with parameter values filled in, e.g. `330Ω`."""
        return _LABEL.sub(lambda m: self.labels.get(m[1], "?"), self.spec.symbol.label)


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


type Analysis = Op | Tran | Ac


def domain_of(analysis: Analysis) -> Domain:
    if isinstance(analysis, Op):
        return "op"
    return "tran" if isinstance(analysis, Tran) else "ac"


@dataclass(frozen=True)
class CompiledExpectation:
    index: int
    measure: Measure
    min: float | None
    max: float | None
    location: Location

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
class CompiledTest:
    name: str
    description: str
    analysis: Analysis
    expectations: tuple[CompiledExpectation, ...]
    location: Location
    pins: dict[str, str] = field(default_factory=lambda: dict[str, str]())  # net → high/low
    firmware: bool = False  # co-simulate the compiled firmware (tran only)
    serial: tuple[SerialExpectation, ...] = ()
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


@dataclass(frozen=True)
class BoardRules:
    clearance: float  # mm
    track_width: float
    via_diameter: float
    via_drill: float


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
