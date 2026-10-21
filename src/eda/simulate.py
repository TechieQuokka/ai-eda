"""Run compiled tests through ngspice and judge the expectations."""

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, JsonValue

from eda import cosim, firmware
from eda.adapters import ngspice, picosdk
from eda.model import (
    GROUND,
    Ac,
    CompiledEvent,
    CompiledExpectation,
    CompiledTest,
    Design,
    Op,
    SerialExpectation,
    domain_of,
)
from eda.netlist.spice import (
    build_netlist,
    current_vector,
    needs_pspice_mode,
    optran_stop,
    pin_current_vector,
    voltage_vector,
)
from eda.result import Diagnostics
from eda.units import format_quantity
from eda.verify.measure import Probe, Trace, evaluate

# ngspice's own fallback (`optran 1 1 1 100n 10u 0` set in cp_init) runs for 10 us.
DEFAULT_OPTRAN_STOP = 10e-6
OPTRAN_STOP_LIMIT = 1.0  # longest transient operating point tried while settling

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


def run_tests(
    design: Design,
    diag: Diagnostics,
    only: str | None = None,
    image: Path | None = None,
    build_firmware: bool = True,
) -> dict[str, JsonValue]:
    """Run the tests. `firmware: true` tests run the UF2 `image`; without one the firmware
    is built here (unless `build_firmware` is False: the caller built it already)."""
    sim_dir = design.paths.out_dir / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    selected = [t for t in design.tests.values() if only is None or t.name == only]
    if only is not None and not selected:
        diag.error("unknown_test", f"no test named {only!r}; tests: {', '.join(design.tests)}")
        return {}
    if image is None and build_firmware and any(t.firmware for t in selected):
        image = _firmware_image(design, diag)

    results: dict[str, JsonValue] = {}
    passed = 0
    for test in selected:
        outcome = _run_one(design, test, sim_dir, diag, image)
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


def _firmware_image(design: Design, diag: Diagnostics) -> Path | None:
    build = firmware.build(design, diag)
    uf2 = build.get("uf2")
    return design.paths.root / uf2 if isinstance(uf2, str) else None


def _run_one(
    design: Design, test: CompiledTest, sim_dir: Path, diag: Diagnostics, image: Path | None
) -> dict[str, JsonValue] | None:
    netlist_path = sim_dir / f"{test.name}.cir"
    raw_path = sim_dir / f"{test.name}.raw"
    log_path = sim_dir / f"{test.name}.log"
    raw_path.unlink(missing_ok=True)
    if test.interactive:
        return _run_cosim(design, test, sim_dir, diag, image)
    circuit = test.circuit(design.circuit)
    rel_log = design.paths.rel(log_path)

    def simulate(optran: float | None) -> ngspice.RunOutput | None:
        netlist = build_netlist(
            f"{design.project.name} / {test.name}",
            circuit,
            test.analysis,
            Path(raw_path.name),
            _drives(design, test),
            optran=optran,
        )
        netlist_path.write_text(netlist, encoding="utf-8")
        try:
            return ngspice.run(netlist_path, log_path, needs_pspice_mode(circuit))
        except ngspice.NgspiceNotFound as exc:
            diag.fail_tool("simulator_missing", str(exc))
        except subprocess.TimeoutExpired:
            diag.fail_tool("simulator_timeout", f"{test.name}: ngspice timed out", log=rel_log)
        return None

    output = simulate(None)
    if output is None:
        return None
    settled: Settling | None = None
    if output.op_via_transient and not output.errors:
        settled = _settle(design, test, sim_dir)
        if settled.stop != (optran_stop(circuit) or DEFAULT_OPTRAN_STOP):
            output = simulate(settled.stop)
            if output is None:
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
    if _singular(test, output.warnings, diag):
        return None
    for warning in output.warnings[:5]:
        diag.warning("simulator_warning", f"{test.name}: {warning}", test.location, log=rel_log)
    if output.op_via_transient:
        _warn_transient_op(design, test, diag, settled)

    return _evaluate(design, test, sim_dir, raw_path, diag, netlist_path)


def _singular(test: CompiledTest, warnings: list[str], diag: Diagnostics) -> bool:
    """A singular matrix means the circuit has no unique solution (a loop of sources and
    inductors, a node held by nothing); ngspice still prints numbers, but they mean
    nothing, so the test is not evaluated."""
    hits = [w for w in warnings if "singular matrix" in w.lower()]
    if not hits:
        return False
    nodes = sorted({m[1] for w in hits if (m := _SINGULAR_NODE.search(w))})
    where = f" near node {', '.join(nodes)}" if nodes else ""
    diag.error(
        "singular_matrix",
        f"{test.name}: the circuit has no unique solution{where} (e.g. a loop of voltage "
        "sources and inductors, or a node that nothing defines); results not evaluated",
        test.location,
        warnings=hits[:5],
    )
    return True


_SINGULAR_NODE = re.compile(r"check node (\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class Settling:
    stop: float  # transient operating-point run time that gives a settled result
    settled: bool  # False: still changing at the longest run tried
    tried: tuple[float, ...]


def _settle(design: Design, test: CompiledTest, sim_dir: Path) -> Settling:
    """Find a transient operating-point run time after which the result stops changing.

    Runs the operating point with 10x longer transients (starting at what the parts ask
    for, else ngspice's 10 us) until two runs agree on every net voltage within 1 mV
    (+0.1 %); the shorter of the two is the answer."""
    circuit = test.circuit(design.circuit)
    first = optran_stop(circuit) or DEFAULT_OPTRAN_STOP
    stops = [first * 10**k for k in range(6) if first * 10**k <= OPTRAN_STOP_LIMIT * 1.001]
    netlist_path = sim_dir / f"{test.name}.settle.cir"
    raw_path = sim_dir / f"{test.name}.settle.raw"
    nets = [(net, vec) for net in circuit.nets if (vec := voltage_vector(net)) is not None]
    key = build_netlist("settling", circuit, Op(), Path("x"), _drives(design, test))
    if key in _settled:
        return _settled[key]
    previous: dict[str, float] | None = None
    tried: list[float] = []
    result = Settling(stop=first, settled=False, tried=())
    for stop in stops:
        raw_path.unlink(missing_ok=True)
        netlist_path.write_text(
            build_netlist(
                f"{design.project.name} / {test.name} (settling {stop:g} s)",
                circuit,
                Op(),
                Path(raw_path.name),
                _drives(design, test),
                optran=stop,
            ),
            encoding="utf-8",
        )
        try:
            output = ngspice.run(
                netlist_path, sim_dir / f"{test.name}.settle.log", needs_pspice_mode(circuit)
            )
            plots = ngspice.parse_raw(raw_path.read_text(encoding="utf-8"))
        except (ngspice.NgspiceNotFound, subprocess.TimeoutExpired, OSError, ValueError):
            break
        if output.errors or not plots:
            break
        tried.append(stop)
        vectors = plots[-1].vectors
        current = {net: vectors[vec][-1] for net, vec in nets if vec in vectors}
        if previous is not None and all(
            abs(current[net] - value) <= 1e-3 + 1e-3 * abs(value)
            for net, value in previous.items()
            if net in current
        ):
            result = Settling(stop=tried[-2], settled=True, tried=tuple(tried))
            break
        previous = current
        result = Settling(stop=stop, settled=False, tried=tuple(tried))
    _settled[key] = result
    return result


# Settling results by circuit netlist: tests on the same circuit search once per process.
_settled: dict[str, Settling] = {}


def _run_cosim(
    design: Design, test: CompiledTest, sim_dir: Path, diag: Diagnostics, image: Path | None
) -> dict[str, JsonValue] | None:
    if test.firmware and (image is None or not image.is_file()):
        try:
            picosdk.sdk_path()
        except picosdk.PicoSdkError as exc:
            diag.fail_tool("fw_toolchain", f"{test.name}: cannot build the firmware: {exc}")
            return None
        diag.error(
            "firmware_not_built",
            f"{test.name}: the firmware did not build; fix the fw_* errors first",
            test.location,
        )
        return None
    raw_path = sim_dir / f"{test.name}.raw"
    try:
        result = cosim.run(design, test, image, raw_path)
    except cosim.CosimError as exc:
        diag.fail_tool("cosim_failed", f"{test.name}: {exc}")
        return None
    if _singular(test, result.warnings, diag):
        return None
    for warning in result.warnings[:5]:
        diag.warning("simulator_warning", f"{test.name}: {warning}", test.location)
    if result.op_via_transient:
        _warn_transient_op(design, test, diag, None)
    outcome = _evaluate(design, test, sim_dir, raw_path, diag, raw_path.with_suffix(".cir"))
    if outcome is None:
        return None
    if test.events:
        outcome["events"] = [_event_json(event) for event in test.events]
    if not test.firmware or image is None:
        return outcome
    uart_path = sim_dir / f"{test.name}.uart.txt"
    serial = {net: result.uart.get(index, "") for net, index in _uart_nets(design).items()}
    uart_path.write_text(
        "".join(f"--- {net} ---\n{text}\n" for net, text in serial.items()), encoding="utf-8"
    )
    checks = outcome["expectations"]
    assert isinstance(checks, list)
    for exp in test.serial:
        check = _judge_serial(exp, serial.get(exp.net, ""), test, diag)
        checks.append(check)
        if check["passed"] is not True:
            outcome["passed"] = False
    if result.faults:
        t, message = result.faults[0]
        diag.error(
            "firmware_fault",
            f"{test.name}: at {format_quantity(t, 's')} the firmware accessed unmapped memory "
            f"({message}); the real chip would take a HardFault, the emulator carried on",
            test.location,
            faults=[[ft, fm] for ft, fm in result.faults],
        )
        outcome["passed"] = False
    if result.halted is not None:
        at = f" at {format_quantity(result.halted_at, 's')}" if result.halted_at is not None else ""
        diag.warning(
            "firmware_halted",
            f"{test.name}: the firmware stopped{at} on a breakpoint (code {result.halted}): "
            "main() returned, panic(), or __breakpoint()",
            test.location,
        )
    outcome["firmware"] = {
        "image": design.paths.rel(image),
        "windows": result.windows,
        "halted": result.halted,
        "halted_at": result.halted_at,
        "faults": [[t, m] for t, m in result.faults],
        "serial": {net: text[-2000:] for net, text in serial.items()},
        "serial_file": design.paths.rel(uart_path),
    }
    return outcome


def _warn_transient_op(
    design: Design, test: CompiledTest, diag: Diagnostics, settled: Settling | None
) -> None:
    circuit = test.circuit(design.circuit)
    asked = [ref for ref, p in circuit.parts.items() if p.spec.spice.optran is not None]
    stop = settled.stop if settled else optran_stop(circuit) or DEFAULT_OPTRAN_STOP
    why = (
        f"as {', '.join(asked)} asks (spice.optran)"
        if asked
        else "because Newton iteration, gmin stepping, and source stepping did not converge"
    )
    shown = format_quantity(stop, "s")
    if settled is None:
        check = "not checked for settling"
    elif settled.settled:
        longer = format_quantity(stop * 10, "s")
        check = f"settled: a {longer} run gives the same net voltages within 1 mV"
    else:
        tried = ", ".join(format_quantity(t, "s") for t in settled.tried)
        check = f"NOT settled: net voltages still change between runs of {tried}"
    hint = (
        f"set `spice: {{ optran: {{ stop: {shown} }} }}` on the vendor part to skip the search"
        if settled and settled.settled and not asked
        else ""
    )
    rule = "op_not_settled" if settled is not None and not settled.settled else "op_via_transient"
    diag.warning(
        rule,
        f"{test.name}: the DC operating point comes from a {shown} transient run from "
        f"power-up ({why}); {check}" + (f"; {hint}" if hint else ""),
        test.location,
        parts=asked,
        optran_stop=stop,
    )


def _event_json(event: CompiledEvent) -> dict[str, JsonValue]:
    what: str
    if event.part is not None:
        what = f"{event.part}: " + ", ".join(
            f"{k}={label}" for k, (_, label) in event.values.items()
        )
    else:
        what = f"{event.net} <- {event.text!r}"
    return {"time": event.time, "display": format_quantity(event.time, "s"), "event": what}


def _uart_nets(design: Design) -> dict[str, int]:
    firmware_ = design.firmware
    if firmware_ is None:
        return {}
    found: dict[str, int] = {}
    for signal in firmware_.signals.values():
        function = signal.function or ""
        if function.startswith("UART") and function.endswith("_TX"):
            found[signal.net] = int(function[4])
    return found


def _judge_serial(
    exp: SerialExpectation, text: str, test: CompiledTest, diag: Diagnostics
) -> dict[str, JsonValue]:
    matches = list(re.finditer(exp.pattern, text))
    value: float | None
    if re.compile(exp.pattern).groups:
        try:
            value = float(matches[-1].group(1)) if matches else None
        except ValueError:
            value = None
    else:
        value = float(len(matches))
    passed = (
        value is not None
        and (exp.min is None or value >= exp.min)
        and (exp.max is None or value <= exp.max)
    )
    display = f"{value:g}" if value is not None else "n/a"
    if not passed:
        last = text.strip().splitlines()[-3:]
        hint = "no match in the serial output" if value is None else "outside the expected range"
        diag.error(
            "expect",
            f"{test.name}: {exp.text} = {display}, expected {exp.range_text} ({hint})",
            exp.location,
            test=test.name,
            measure=exp.text,
            value=value,
            min=exp.min,
            max=exp.max,
            serial_tail=last,
        )
    return {
        "measure": exp.text,
        "value": value,
        "display": display,
        "unit": "",
        "min": exp.min,
        "max": exp.max,
        "expected": exp.range_text,
        "passed": passed,
        "line": exp.location.line,
    }


def _evaluate(
    design: Design,
    test: CompiledTest,
    sim_dir: Path,
    raw_path: Path,
    diag: Diagnostics,
    netlist_path: Path | None,
) -> dict[str, JsonValue] | None:
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
    if not isinstance(test.analysis, Ac) and _reversed_supplies(design, test, traces, diag):
        all_passed = False

    outcome: dict[str, JsonValue] = {
        "analysis": domain_of(test.analysis),
        "passed": all_passed,
        "expectations": checks,
        "netlist": design.paths.rel(netlist_path) if netlist_path else None,
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


REVERSED_SUPPLY_V = 0.1  # v- this far above v+ means the rails are swapped


def _reversed_supplies(
    design: Design, test: CompiledTest, traces: Traces, diag: Diagnostics
) -> bool:
    """An op-amp whose v- rail sits above its v+ rail: models still compute an output, a
    real part would not work (or burn). Checked over the whole run."""

    def volts(net: str | None) -> Values | None:
        if net == GROUND:
            return [0.0] * traces.points
        found = traces.signals.get(f"voltage({net})")
        return found[1] if found else None

    reversed_any = False
    for ref, part in design.circuit.parts.items():
        if part.spec.category != "opamp" or not {"v+", "v-"} <= set(part.spec.pins):
            continue
        plus = volts(design.circuit.net_of(ref, "v+"))
        minus = volts(design.circuit.net_of(ref, "v-"))
        if plus is None or minus is None:
            continue
        worst = max(_real(m) - _real(p) for p, m in zip(plus, minus, strict=True))
        if worst > REVERSED_SUPPLY_V:
            diag.error(
                "supply_reversed",
                f"{test.name}: {ref}'s v- supply is {format_quantity(worst, 'V')} above its v+ "
                "supply; check the rail connections (v+ to the positive rail)",
                design.circuit.source.at("parts", ref),
            )
            reversed_any = True
    return reversed_any


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
    lo, hi = _window(traces.x, exp.start, exp.end)

    def lookup(probe: Probe) -> Trace | None:
        values = probe_values(probe, traces.signals, traces.points)
        return values[lo:hi] if values is not None else None

    try:
        value = evaluate(
            measure.node,
            domain_of(test.analysis),
            traces.x[lo:hi] if traces.x is not None else None,
            lookup,
        )
    except KeyError as exc:
        diag.fail_tool("missing_vector", f"{test.name}: simulator did not report {exc.args[0]}")
        return {"measure": exp.label, "passed": False, "value": None}
    passed = (
        math.isfinite(value)
        and (exp.min is None or value >= exp.min)
        and (exp.max is None or value <= exp.max)
    )
    display = format_quantity(value, unit) if math.isfinite(value) else "n/a"
    if not passed:
        if not math.isfinite(value):
            hint = {
                "cross": "the trace never crosses that level",
                "period": "the trace does not rise through that level twice",
                "duty": "the trace does not rise through that level twice",
                "width": "no complete pulse above that level",
                "gap": "no complete gap below that level",
            }.get(measure.reduction or "", "outside the simulated range")
        elif exp.min is not None and value < exp.min:
            hint = f"too low by {format_quantity(exp.min - value, unit)}"
        else:
            assert exp.max is not None
            hint = f"too high by {format_quantity(value - exp.max, unit)}"
        diag.error(
            "expect",
            f"{test.name}: {exp.label} = {display}, expected {exp.range_text} ({hint})",
            exp.location,
            test=test.name,
            measure=measure.text,
            value=value if math.isfinite(value) else None,
            min=exp.min,
            max=exp.max,
            unit=unit,
        )
    return {
        "measure": exp.label,
        "value": value if math.isfinite(value) else None,
        "display": display,
        "unit": unit,
        "min": exp.min,
        "max": exp.max,
        "expected": exp.range_text,
        "passed": passed,
        "line": exp.location.line,
    }


def _window(
    x: list[float] | None, start: float | None, end: float | None
) -> tuple[int, int | None]:
    """Slice bounds of the samples within [start, end] (the whole trace when unset)."""
    if x is None or (start is None and end is None):
        return 0, None
    lo = next((i for i, t in enumerate(x) if start is None or t >= start - 1e-15), len(x))
    hi = next((i for i, t in enumerate(x) if end is not None and t > end + 1e-15), len(x))
    return lo, hi


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
