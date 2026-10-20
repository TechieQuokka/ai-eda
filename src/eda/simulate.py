"""Run compiled tests through ngspice and judge the expectations."""

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, JsonValue

from eda.adapters import ngspice
from eda.model import GROUND, CompiledExpectation, CompiledTest, Design, Op
from eda.netlist.spice import build_netlist, current_vector, voltage_vector
from eda.result import Diagnostics
from eda.units import format_quantity
from eda.verify.measure import Probe, reduce


class Signal(BaseModel):
    unit: str
    values: list[float]


class WaveformData(BaseModel):
    """`out/sim/<test>.json` for transient tests."""

    time: list[float]
    signals: dict[str, Signal]

    def as_signals(self) -> dict[str, tuple[str, list[float]]]:
        return {name: (s.unit, s.values) for name, s in self.signals.items()}


@dataclass(frozen=True)
class Traces:
    time: list[float] | None
    vectors: dict[str, list[float]]
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
        ),
        encoding="utf-8",
    )
    rel_log = design.paths.rel(log_path)
    try:
        output = ngspice.run(netlist_path, log_path)
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
    time = plot.vectors.get("time")
    traces = Traces(time=time, vectors=plot.vectors, points=len(next(iter(plot.vectors.values()))))

    signals = _named_signals(design, traces)
    checks: list[JsonValue] = []
    all_passed = True
    for exp in test.expectations:
        check = _judge(exp, traces, signals, test, diag)
        all_passed = all_passed and check["passed"] is True
        checks.append(check)

    outcome: dict[str, JsonValue] = {
        "analysis": "op" if isinstance(test.analysis, Op) else "tran",
        "passed": all_passed,
        "expectations": checks,
        "netlist": design.paths.rel(netlist_path),
    }
    if isinstance(test.analysis, Op):
        outcome["operating_point"] = {
            name: {"value": values[-1], "display": format_quantity(values[-1], unit)}
            for name, (unit, values) in signals.items()
        }
    else:
        wave_path = sim_dir / f"{test.name}.json"
        wave_path.write_text(_waveform_json(traces, signals), encoding="utf-8")
        outcome["waveform"] = design.paths.rel(wave_path)
    return outcome


def _named_signals(design: Design, traces: Traces) -> dict[str, tuple[str, list[float]]]:
    """Every net voltage and part current under the names tests use."""
    zeros = [0.0] * traces.points
    signals: dict[str, tuple[str, list[float]]] = {}
    for net in design.circuit.nets:
        vec = voltage_vector(net)
        values = zeros if vec is None else traces.vectors.get(vec)
        if values is not None and net != GROUND:
            signals[f"voltage({net})"] = ("V", values)
    for ref, part in design.circuit.parts.items():
        values = traces.vectors.get(current_vector(part))
        if values is not None:
            signals[f"current({ref})"] = ("A", values)
    return signals


def probe_values(
    probe: Probe, signals: dict[str, tuple[str, list[float]]], points: int
) -> list[float] | None:
    if probe.kind == "current":
        found = signals.get(f"current({probe.targets[0]})")
        return found[1] if found else None
    series: list[list[float]] = []
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
    return [a - b for a, b in zip(series[0], series[1], strict=True)]


def _judge(
    exp: CompiledExpectation,
    traces: Traces,
    signals: dict[str, tuple[str, list[float]]],
    test: CompiledTest,
    diag: Diagnostics,
) -> dict[str, JsonValue]:
    measure = exp.measure
    unit = measure.probe.unit
    values = probe_values(measure.probe, signals, traces.points)
    if values is None:
        diag.fail_tool("missing_vector", f"{test.name}: simulator did not report {measure.text}")
        return {"measure": measure.text, "passed": False, "value": None}
    value = reduce(measure.reduction, traces.time, values)
    passed = (
        math.isfinite(value)
        and (exp.min is None or value >= exp.min)
        and (exp.max is None or value <= exp.max)
    )
    display = format_quantity(value, unit)
    if not passed:
        if exp.min is not None and value < exp.min:
            hint = f"too low by {format_quantity(exp.min - value, unit)}"
        elif exp.max is not None and value > exp.max:
            hint = f"too high by {format_quantity(value - exp.max, unit)}"
        else:
            hint = "not a finite number"
        diag.error(
            "expect",
            f"{test.name}: {measure.text} = {display}, expected {exp.range_text} ({hint})",
            exp.location,
            test=test.name,
            measure=measure.text,
            value=value,
            min=exp.min,
            max=exp.max,
            unit=unit,
        )
    return {
        "measure": measure.text,
        "value": value,
        "display": display,
        "unit": unit,
        "min": exp.min,
        "max": exp.max,
        "expected": exp.range_text,
        "passed": passed,
        "line": exp.location.line,
    }


def _waveform_json(traces: Traces, signals: dict[str, tuple[str, list[float]]]) -> str:
    data = WaveformData(
        time=list(traces.time or []),
        signals={name: Signal(unit=unit, values=v) for name, (unit, v) in signals.items()},
    )
    return data.model_dump_json()
