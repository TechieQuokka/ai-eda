"""Measure expressions in tests.

Grammar (whitespace is free):

    expr  := probe | NAME "(" expr ["," QUANTITY] ")"
    probe := voltage(NET) | voltage(NET_A, NET_B) | current(REF) | current(REF.PIN)

Functions by analysis:

    reductions  max min initial final           op, tran, ac   trace → value
                avg rms pp                      op, tran       trace → value
    sampling    at(trace, X)                    tran, ac       value at time / frequency X
                cross(trace, LEVEL)             tran, ac       first time / frequency the
                                                               trace crosses LEVEL
                period(trace, LEVEL)            tran           mean time between rising
                                                               crossings of LEVEL
    transforms  db mag phase                    ac             complex trace → real trace

A `tran` measure must end in a value; an `ac` probe must pass through db(), mag(), or
phase(). In `op`, a bare probe is its value.
"""

import cmath
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from eda.units import QuantityError, parse_any_quantity

type Domain = Literal["op", "tran", "ac"]

REDUCTIONS: Final = ("max", "min", "initial", "final", "avg", "rms", "pp")
TIME_ONLY: Final = frozenset({"avg", "rms", "pp"})
TRANSFORMS: Final = {"db": "dB", "mag": None, "phase": "deg"}
SAMPLERS: Final = ("at", "cross", "period")
X_UNIT: Final[dict[Domain, str | None]] = {"op": None, "tran": "s", "ac": "Hz"}

_TOKEN: Final = re.compile(r"\s*(?:(?P<open>\()|(?P<close>\))|(?P<comma>,)|(?P<atom>[^\s(),]+))")
_IDENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PIN_REF: Final = re.compile(r"^(?P<ref>[A-Za-z_][A-Za-z0-9_]*)\.(?P<pin>[^.\s]+)$")


class MeasureSyntaxError(ValueError):
    pass


class MeasureTypeError(ValueError):
    pass


@dataclass(frozen=True)
class Probe:
    kind: Literal["voltage", "current"]
    targets: tuple[str, ...]  # nets for voltage; one part reference for current
    pin: str | None = None  # current into one pin of a multi-pin part

    @property
    def unit(self) -> str:
        return "V" if self.kind == "voltage" else "A"

    @property
    def text(self) -> str:
        if self.kind == "current" and self.pin is not None:
            return f"current({self.targets[0]}.{self.pin})"
        return f"{self.kind}({', '.join(self.targets)})"


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str
    text: str


@dataclass(frozen=True)
class Call:
    fn: str
    arg: "Node"
    quantity: Quantity | None = None

    @property
    def text(self) -> str:
        inner = self.arg.text
        return (
            f"{self.fn}({inner}, {self.quantity.text})" if self.quantity else f"{self.fn}({inner})"
        )


type Node = Probe | Call


@dataclass(frozen=True)
class Measure:
    node: Node
    unit: str  # unit of the final value

    @property
    def text(self) -> str:
        return self.node.text

    @property
    def probe(self) -> Probe:
        node = self.node
        while isinstance(node, Call):
            node = node.arg
        return node

    @property
    def reduction(self) -> str | None:
        """Outermost function name (None for a bare probe)."""
        return self.node.fn if isinstance(self.node, Call) else None


# --- parsing ----------------------------------------------------------------------------


MAX_NESTING = 20  # function calls inside one another in a measure


def parse_measure(text: str) -> Node:
    tokens = _tokenize(text)
    depth = deepest = 0
    for kind, _ in tokens:
        depth += {"open": 1, "close": -1}.get(kind, 0)
        deepest = max(deepest, depth)
    if deepest > MAX_NESTING:
        raise MeasureSyntaxError(f"{text[:40]!r}...: nested more than {MAX_NESTING} levels deep")
    node, rest = _parse_node(tokens, text)
    if rest:
        raise MeasureSyntaxError(f"unexpected {rest[0][1]!r} in {text!r}")
    return node


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None or match.end() == pos:
            if text[pos:].strip() == "":
                break
            raise MeasureSyntaxError(f"cannot read {text[pos:]!r} in {text!r}")
        kind = match.lastgroup
        if kind is not None:
            tokens.append((kind, match[kind]))
        pos = match.end()
    if not tokens:
        raise MeasureSyntaxError(_usage(text))
    return tokens


def _parse_node(tokens: list[tuple[str, str]], text: str) -> tuple[Node, list[tuple[str, str]]]:
    if len(tokens) < 3 or tokens[0][0] != "atom" or tokens[1][0] != "open":
        raise MeasureSyntaxError(_usage(text))
    name = tokens[0][1]
    rest = tokens[2:]
    if name in ("voltage", "current"):
        args: list[str] = []
        while rest and rest[0][0] == "atom":
            args.append(rest[0][1])
            rest = rest[1:]
            if rest and rest[0][0] == "comma":
                rest = rest[1:]
        if not rest or rest[0][0] != "close":
            raise MeasureSyntaxError(_usage(text))
        return _probe(name, args, text), rest[1:]

    if name not in REDUCTIONS and name not in TRANSFORMS and name not in SAMPLERS:
        known = ", ".join((*REDUCTIONS, *TRANSFORMS, *SAMPLERS))
        raise MeasureSyntaxError(f"unknown function {name!r} in {text!r}; functions: {known}")
    arg, rest = _parse_node(rest, text)
    quantity: Quantity | None = None
    if rest and rest[0][0] == "comma":
        if len(rest) < 2 or rest[1][0] != "atom":
            raise MeasureSyntaxError(f"{name}() expects a quantity after the comma in {text!r}")
        try:
            value, unit = parse_any_quantity(rest[1][1])
        except QuantityError as exc:
            raise MeasureSyntaxError(f"{name}(): {exc}") from exc
        quantity = Quantity(value, unit, rest[1][1])
        rest = rest[2:]
    if not rest or rest[0][0] != "close":
        raise MeasureSyntaxError(f"missing ')' after {name}(...) in {text!r}")
    if name in SAMPLERS and quantity is None:
        hint = "at(db(voltage(OUT)), 1kHz)" if name == "at" else f"{name}(voltage(OUT), 2.5V)"
        raise MeasureSyntaxError(f"{name}() needs a second argument, e.g. {hint}")
    if name not in SAMPLERS and quantity is not None:
        raise MeasureSyntaxError(f"{name}() takes one argument in {text!r}")
    return Call(name, arg, quantity), rest[1:]


def _probe(kind: str, args: list[str], text: str) -> Probe:
    if kind == "voltage":
        if not 1 <= len(args) <= 2 or not all(_IDENT.match(a) for a in args):
            raise MeasureSyntaxError(f"voltage() takes one or two net names: {text!r}")
        return Probe("voltage", tuple(args))
    if len(args) != 1:
        raise MeasureSyntaxError(f"current() takes one part reference or REF.PIN: {text!r}")
    if _IDENT.match(args[0]):
        return Probe("current", (args[0],))
    match = _PIN_REF.match(args[0])
    if match is None:
        raise MeasureSyntaxError(f"current() takes one part reference or REF.PIN: {text!r}")
    return Probe("current", (match["ref"],), match["pin"])


def _usage(text: str) -> str:
    return (
        f"invalid measure {text!r}: expected voltage(NET), voltage(NET_A, NET_B), "
        "current(REF), or current(REF.PIN), optionally inside functions such as "
        "max(...), at(..., 1ms), db(...)"
    )


# --- type checking ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Type:
    shape: Literal["trace", "value"]
    unit: str
    complex: bool = False


def check(node: Node, domain: Domain) -> Measure:
    """Validate a parsed measure for an analysis and return it with its result unit."""
    result = _check(node, domain)
    if result.shape == "trace":
        if domain == "tran":
            raise MeasureTypeError(
                f"{node.text}: a tran analysis gives a waveform; reduce it with max(), min(), "
                "avg(), rms(), pp(), initial(), final(), at(..., TIME), cross(..., LEVEL), "
                "or period(..., LEVEL)"
            )
        if domain == "ac":
            raise MeasureTypeError(
                f"{node.text}: an ac analysis gives a frequency response; use e.g. "
                f"at(db({node.text}), 1kHz), max(db(...)), or cross(db(...), -3dB)"
            )
    return Measure(node, result.unit)


def _check(node: Node, domain: Domain) -> _Type:
    if isinstance(node, Probe):
        return _Type("trace", node.unit, complex=domain == "ac")
    inner = _check(node.arg, domain)
    fn = node.fn
    if fn in TRANSFORMS:
        if domain != "ac":
            raise MeasureTypeError(f"{node.text}: {fn}() applies to ac analyses only")
        if inner.shape != "trace" or not inner.complex:
            raise MeasureTypeError(f"{node.text}: {fn}() takes a probe such as voltage(OUT)")
        return _Type("trace", TRANSFORMS[fn] or inner.unit)
    if inner.shape != "trace":
        raise MeasureTypeError(f"{node.text}: {fn}() needs a waveform, not a single value")
    if inner.complex:
        raise MeasureTypeError(
            f"{node.text}: wrap the probe in db(), mag(), or phase() before {fn}()"
        )
    if fn in TIME_ONLY and domain == "ac":
        raise MeasureTypeError(f"{node.text}: {fn}() is not meaningful over frequency")
    if fn in SAMPLERS:
        x_unit = X_UNIT[domain]
        if x_unit is None:
            raise MeasureTypeError(f"{node.text}: {fn}() needs a tran or ac analysis")
        if fn == "period" and domain != "tran":
            raise MeasureTypeError(f"{node.text}: period() needs a tran analysis")
        assert node.quantity is not None
        expected = x_unit if fn == "at" else inner.unit
        if node.quantity.unit != expected:
            raise MeasureTypeError(
                f"{node.text}: second argument of {fn}() must be in {expected}, "
                f"got {node.quantity.text!r}"
            )
        return _Type("value", inner.unit if fn == "at" else x_unit)
    return _Type("value", inner.unit)


def probes(node: Node) -> list[Probe]:
    return [node] if isinstance(node, Probe) else probes(node.arg)


# --- evaluation -------------------------------------------------------------------------

type Trace = Sequence[float] | Sequence[complex]


def evaluate(
    node: Node,
    domain: Domain,
    x: Sequence[float] | None,
    lookup: Callable[[Probe], Trace | None],
) -> float:
    """Evaluate a checked measure. Raises KeyError if a probe has no data."""
    value = _eval(node, domain, x, lookup)
    if isinstance(value, (int, float)):
        return float(value)
    real = _real(value)
    return real[-1] if real else math.nan


def _eval(
    node: Node,
    domain: Domain,
    x: Sequence[float] | None,
    lookup: Callable[[Probe], Trace | None],
) -> Trace | float:
    if isinstance(node, Probe):
        data = lookup(node)
        if data is None:
            raise KeyError(node.text)
        return data
    inner = _eval(node.arg, domain, x, lookup)
    assert not isinstance(inner, (int, float)), "checked measures never reduce twice"
    if node.fn == "db":
        return db_values(inner)
    if node.fn == "mag":
        return [abs(v) for v in inner]
    if node.fn == "phase":
        return phase_values(inner)
    values = _real(inner)
    if node.fn in SAMPLERS:
        assert x is not None and node.quantity is not None
        xs = [math.log10(v) for v in x] if domain == "ac" else list(x)
        if node.fn == "at":
            target = math.log10(node.quantity.value) if domain == "ac" else node.quantity.value
            return _interpolate(xs, values, target)
        if node.fn == "period":
            return _period(xs, values, node.quantity.value)
        found = _crossing(xs, values, node.quantity.value)
        return 10**found if domain == "ac" and math.isfinite(found) else found
    return reduce(node.fn, x if domain == "tran" else None, values)


def db_values(values: Trace) -> list[float]:
    return [20 * math.log10(max(abs(v), 1e-300)) for v in values]


def phase_values(values: Trace) -> list[float]:
    """Phase in degrees, unwrapped so it does not jump by 360°."""
    return _unwrap([math.degrees(cmath.phase(complex(v))) for v in values])


def _real(values: Trace) -> list[float]:
    return [v.real if isinstance(v, complex) else float(v) for v in values]


def _unwrap(degrees: list[float]) -> list[float]:
    out: list[float] = []
    offset = 0.0
    for i, value in enumerate(degrees):
        if i:
            step = value + offset - out[-1]
            if step > 180:
                offset -= 360
            elif step < -180:
                offset += 360
        out.append(value + offset)
    return out


def _interpolate(xs: Sequence[float], ys: Sequence[float], target: float) -> float:
    if not xs or target < xs[0] or target > xs[-1]:
        return math.nan
    for i in range(1, len(xs)):
        if xs[i] >= target:
            span = xs[i] - xs[i - 1]
            t = 0.0 if span == 0 else (target - xs[i - 1]) / span
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def _crossing(xs: Sequence[float], ys: Sequence[float], level: float) -> float:
    for i in range(1, len(xs)):
        a, b = ys[i - 1] - level, ys[i] - level
        if a == 0:
            return xs[i - 1]
        if a * b < 0:
            return xs[i - 1] + (xs[i] - xs[i - 1]) * a / (a - b)
    return math.nan


def _period(xs: Sequence[float], ys: Sequence[float], level: float) -> float:
    """Mean spacing of the times the trace rises through `level` (NaN below two rises)."""
    rises: list[float] = []
    for i in range(1, len(xs)):
        a, b = ys[i - 1] - level, ys[i] - level
        if a < 0 <= b:
            rises.append(xs[i - 1] + (xs[i] - xs[i - 1]) * a / (a - b))
    if len(rises) < 2:
        return math.nan
    return (rises[-1] - rises[0]) / (len(rises) - 1)


def reduce(reduction: str | None, time: Sequence[float] | None, values: Sequence[float]) -> float:
    """Reduce a trace to one number. `time` is None for single-point analyses."""
    if not values:
        return math.nan
    if reduction is None or reduction == "final":
        return values[-1]
    if reduction == "initial":
        return values[0]
    if reduction == "max":
        return max(values)
    if reduction == "min":
        return min(values)
    if reduction == "pp":
        return max(values) - min(values)
    if reduction in ("avg", "rms"):
        squared = reduction == "rms"
        samples = [v * v for v in values] if squared else list(values)
        if time is None or len(values) < 2 or time[-1] == time[0]:
            mean = sum(samples) / len(samples)
        else:
            area = sum(
                (time[i + 1] - time[i]) * (samples[i] + samples[i + 1]) / 2
                for i in range(len(samples) - 1)
            )
            mean = area / (time[-1] - time[0])
        return math.sqrt(mean) if squared else mean
    raise ValueError(f"unknown reduction {reduction!r}")
