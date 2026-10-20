"""Measure expressions in tests: `voltage(N)`, `voltage(A, B)`, `current(R1)`, `max(...)`."""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

REDUCTIONS: Final = ("max", "min", "avg", "rms", "pp", "initial", "final")

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_PROBE_RE: Final = re.compile(
    rf"^(?P<kind>voltage|current)\(\s*(?P<a>{_IDENT})\s*(?:,\s*(?P<b>{_IDENT})\s*)?\)$"
)
_REDUCED_RE: Final = re.compile(r"^(?P<fn>[a-z]+)\((?P<inner>.*)\)$")


class MeasureSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class Probe:
    kind: Literal["voltage", "current"]
    targets: tuple[str, ...]  # nets for voltage, one part reference for current

    @property
    def unit(self) -> str:
        return "V" if self.kind == "voltage" else "A"

    @property
    def text(self) -> str:
        return f"{self.kind}({', '.join(self.targets)})"


@dataclass(frozen=True)
class Measure:
    probe: Probe
    reduction: str | None

    @property
    def text(self) -> str:
        return f"{self.reduction}({self.probe.text})" if self.reduction else self.probe.text


def parse_measure(text: str) -> Measure:
    source = text.strip()
    probe = _parse_probe(source)
    if probe is not None:
        return Measure(probe, None)
    match = _REDUCED_RE.match(source)
    if match is None:
        raise MeasureSyntaxError(_usage(text))
    fn = match["fn"]
    if fn not in REDUCTIONS:
        raise MeasureSyntaxError(f"unknown function {fn!r} in {text!r}; use one of {REDUCTIONS}")
    probe = _parse_probe(match["inner"].strip())
    if probe is None:
        raise MeasureSyntaxError(_usage(text))
    return Measure(probe, fn)


def _parse_probe(text: str) -> Probe | None:
    match = _PROBE_RE.match(text)
    if match is None:
        return None
    if match["kind"] == "current":
        if match["b"] is not None:
            raise MeasureSyntaxError(f"current() takes one part reference: {text!r}")
        return Probe("current", (match["a"],))
    targets = (match["a"],) if match["b"] is None else (match["a"], match["b"])
    return Probe("voltage", targets)


def _usage(text: str) -> str:
    return (
        f"invalid measure {text!r}: expected voltage(NET), voltage(NET_A, NET_B), "
        f"current(REF), optionally wrapped in {', '.join(REDUCTIONS)}()"
    )


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
