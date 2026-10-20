"""Run compiled tests through ngspice and judge the expectations."""

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, JsonValue

from eda.adapters import ngspice
from eda.model import GROUND, Ac, CompiledExpectation, CompiledTest, Design, Op, domain_of
from eda.netlist.spice import (
    build_netlist,
    current_vector,
    needs_pspice_mode,
    pin_current_vector,
    voltage_vector,
)
from eda.result import Diagnostics
from eda.units import format_quantity
from eda.verify.measure import Probe, Trace, evaluate

type Values = list[float] | list[complex]
type Signals = dict[str, tuple[str, Values]]


class Signal(BaseModel):
    unit: str
    values: list[float]  # real part
    imag: list[float] | None = None  # imaginary part (ac only)

    def data(self) -> Values:
        if self.imag is None:
            return self.values
        return [complex(re, im) for re, im in zip(self.values, self.imag, strict=True)]


class WaveformData(BaseModel):
    """`out/sim/<test>.json` for tran (x = time) and ac (x = frequency) tests."""

    axis: Literal["time", "frequency"]
    x: list[float]
    signals: dict[str, Signal]

    def as_signals(self) -> Signals:
        return {name: (s.unit, s.data()) for name, s in self.signals.items()}


@dataclass(frozen=True)
class Traces:
    x: list[float] | None  # time or frequency; None for op
    signals: Signals
    points: int


def run_tests(design: Design, diag: Diagnostics, only: str | None = None) -> dict[str, JsonValue]:
    sim_dir = design.paths.out_dir / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    selected = [t for t in design.tests.values() if only is None or t.name == only]
    if only is not None and not selected:
        diag.error("unknown_test", f"no test named {only!r}; tests: {', '.join(design.tests)}")
        return {}

    results: dict[str, JsonValue] = {}
    passed = 0
    for test in selected:
        outcome = _run_one(design, test, sim_dir, diag)
        if outcome is None:
            continue
        results[test.name] = outcome
        if outcome["passed"] is True:
            passed += 1
    return {
        "simulator": ngspice.version(),
        "summary": {"tests": len(selected), "passed": passed, "failed": len(selected) - passed},
        "tests": results,
    }


def _run_one(
    design: Design, test: CompiledTest, sim_dir: Path, diag: Diagnostics
) -> dict[str, JsonValue] | None:
    netlist_path = sim_dir / f"{test.name}.cir"
    raw_path = sim_dir / f"{test.name}.raw"
    log_path = sim_dir / f"{test.name}.log"
    raw_path.unlink(missing_ok=True)
    netlist_path.write_text(
        build_netlist(
            f"{design.project.name} / {test.name}",
            design.circuit,
            test.analysis,
            Path(raw_path.name),
            _drives(design, test),
        ),
        encoding="utf-8",
    )
    rel_log = design.paths.rel(log_path)
    try:
        output = ngspice.run(netlist_path, log_path, needs_pspice_mode(design.circuit))
    except ngspice.NgspiceNotFound as exc:
        diag.fail_tool("simulator_missing", str(exc))
        return None
    except subprocess.TimeoutExpired:
        diag.fail_tool("simulator_timeout", f"{test.name}: ngspice timed out", log=rel_log)
        return None

    if output.errors or not raw_path.is_file():
        messages = output.errors or [f"ngspice exited with {output.returncode} and no results"]
        diag.error(
            "simulation_failed",
            f"{test.name}: " + "; ".join(messages[:5]),
            test.location,
            log=rel_log,
            netlist=design.paths.rel(netlist_path),
        )
        return None
    for warning in output.warnings[:5]:
        diag.warning("simulator_warning", f"{test.name}: {warning}", test.location, log=rel_log)

    try:
        plots = ngspice.parse_raw(raw_path.read_text(encoding="utf-8"))
    except (ValueError, KeyError, IndexError) as exc:
        diag.fail_tool("raw_parse", f"{test.name}: cannot parse ngspice output: {exc}")
        return None
    if not plots:
        diag.fail_tool("raw_parse", f"{test.name}: ngspice produced no data")
        return None
    plot = plots[-1]
    x = plot.vectors.get("frequency" if isinstance(test.analysis, Ac) else "time")
    vectors: dict[str, Values] = {**plot.vectors, **plot.complex_vectors}
    points = len(next(iter(vectors.values())))
    traces = Traces(x=x, signals=_named_signals(design, vectors, points), points=points)

    checks: list[JsonValue] = []
    all_passed = True
    for exp in test.expectations:
        check = _judge(exp, traces, test, diag)
        all_passed = all_passed and check["passed"] is True
        checks.append(check)

    outcome: dict[str, JsonValue] = {
        "analysis": domain_of(test.analysis),
        "passed": all_passed,
        "expectations": checks,
        "netlist": design.paths.rel(netlist_path),
    }
    if isinstance(test.analysis, Op):
        outcome["operating_point"] = {
            name: {"value": _real(values[-1]), "display": format_quantity(_real(values[-1]), unit)}
            for name, (unit, values) in traces.signals.items()
        }
    else:
        wave_path = sim_dir / f"{test.name}.json"
        axis: Literal["time", "frequency"] = (
            "frequency" if isinstance(test.analysis, Ac) else "time"
        )
        wave_path.write_text(_waveform_json(axis, traces), encoding="utf-8")
        outcome["waveform"] = design.paths.rel(wave_path)
    return outcome


def _drives(design: Design, test: CompiledTest) -> dict[str, dict[str, str]]:
    firmware = design.firmware
    if firmware is None:
        return {}
    return {firmware.mcu: firmware.drives(test.pins)}


def _real(value: float | complex) -> float:
    return value.real if isinstance(value, complex) else value


def _named_signals(design: Design, vectors: dict[str, Values], points: int) -> Signals:
    """Every net voltage, part current, and pin current under the names tests use."""
    signals: Signals = {}
    for net in design.circuit.nets:
        vec = voltage_vector(net)
        values = vectors.get(vec) if vec is not None else None
        if values is not None and net != GROUND:
            signals[f"voltage({net})"] = ("V", values)
    for ref, part in design.circuit.parts.items():
        vec = current_vector(part)
        if vec is not None and (values := vectors.get(vec)) is not None:
            signals[f"current({ref})"] = ("A", values)
        if part.spec.spice.pin_currents:
            for pin in part.spec.pins:
                found = vectors.get(pin_current_vector(part, pin))
                if found is not None:
                    signals[f"current({ref}.{pin})"] = ("A", found)
    return signals


def probe_values(probe: Probe, signals: Signals, points: int) -> Trace | None:
    """Values of one probe; voltage differences are computed, GND reads 0 V."""
    if probe.kind == "current":
        found = signals.get(probe.text)
        return found[1] if found else None
    series: list[Values] = []
    for net in probe.targets:
        if net == GROUND:
            series.append([0.0] * points)
            continue
        found = signals.get(f"voltage({net})")
        if found is None:
            return None
        series.append(found[1])
    if len(series) == 1:
        return series[0]
    return [complex(a) - complex(b) for a, b in zip(series[0], series[1], strict=True)]


def _judge(
    exp: CompiledExpectation,
    traces: Traces,
    test: CompiledTest,
    diag: Diagnostics,
) -> dict[str, JsonValue]:
    measure = exp.measure
    unit = measure.unit
    try:
        value = evaluate(
            measure.node,
            domain_of(test.analysis),
            traces.x,
            lambda probe: probe_values(probe, traces.signals, traces.points),
        )
    except KeyError as exc:
        diag.fail_tool("missing_vector", f"{test.name}: simulator did not report {exc.args[0]}")
        return {"measure": measure.text, "passed": False, "value": None}
    passed = (
        math.isfinite(value)
        and (exp.min is None or value >= exp.min)
        and (exp.max is None or value <= exp.max)
    )
    display = format_quantity(value, unit) if math.isfinite(value) else "n/a"
    if not passed:
        if not math.isfinite(value):
            hint = (
                "the trace never crosses that level"
                if measure.reduction == "cross"
                else "outside the simulated range"
            )
        elif exp.min is not None and value < exp.min:
            hint = f"too low by {format_quantity(exp.min - value, unit)}"
        else:
            assert exp.max is not None
            hint = f"too high by {format_quantity(value - exp.max, unit)}"
        diag.error(
            "expect",
            f"{test.name}: {measure.text} = {display}, expected {exp.range_text} ({hint})",
            exp.location,
            test=test.name,
            measure=measure.text,
            value=value if math.isfinite(value) else None,
            min=exp.min,
            max=exp.max,
            unit=unit,
        )
    return {
        "measure": measure.text,
        "value": value if math.isfinite(value) else None,
        "display": display,
        "unit": unit,
        "min": exp.min,
        "max": exp.max,
        "expected": exp.range_text,
        "passed": passed,
        "line": exp.location.line,
    }


def _waveform_json(axis: Literal["time", "frequency"], traces: Traces) -> str:
    signals: dict[str, Signal] = {}
    for name, (unit, values) in traces.signals.items():
        if axis == "frequency":
            cvalues = [complex(v) for v in values]
            signals[name] = Signal(
                unit=unit, values=[v.real for v in cvalues], imag=[v.imag for v in cvalues]
            )
        else:
            signals[name] = Signal(unit=unit, values=[_real(v) for v in values])
    data = WaveformData(axis=axis, x=list(traces.x or []), signals=signals)
    return data.model_dump_json()
