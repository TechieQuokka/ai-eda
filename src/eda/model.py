"""Resolved, checked design objects produced by `eda.design`."""

from dataclasses import dataclass

from eda.loader import SourceMap
from eda.project import ProjectPaths
from eda.result import Location
from eda.schema.part import PartSpec
from eda.schema.project import Project
from eda.units import format_quantity
from eda.verify.measure import Domain, Measure

GROUND = "GND"

type PinKey = tuple[str, str]  # (part reference, pin name)


@dataclass(frozen=True)
class ResolvedPart:
    ref: str
    spec: PartSpec
    values: dict[str, float | str]  # SI floats for quantities, strings for choices
    labels: dict[str, str]  # human-readable parameter values
    spice_name: str


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
class CompiledTest:
    name: str
    description: str
    analysis: Analysis
    expectations: tuple[CompiledExpectation, ...]
    location: Location


@dataclass(frozen=True)
class Design:
    paths: ProjectPaths
    project: Project
    circuit: ResolvedCircuit
    tests: dict[str, CompiledTest]
    parts: dict[str, PartSpec]
