"""`eda try`: run the circuit (and its firmware) with inputs given on the command line,
without editing design/test.yaml. Nothing is judged and nothing goes into the iteration
log; the run's screens, UART output, and measured values are reported.

The run is an ordinary test, named `try`, built from the options and checked with the
same rules as a test in test.yaml: `--from TEST` starts from a test's analysis, part
settings, and events; `--keys` presses keys mapped in design/inputs.yaml; `--event` and
`--send` add part changes and UART input at given times; `--set` fixes part parameters
for the whole run; `--measure` reports a value.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue, ValidationError

from eda import simulate
from eda.design import compile_tests, key_timing, load_design, split_keys
from eda.loader import SourceMap, load_yaml
from eda.model import Design, Tran
from eda.result import Diagnostics, Location, Result
from eda.schema.testspec import (
    Expectation,
    SimEvent,
    SimTest,
    SimTestFile,
    TranAnalysis,
    TryTest,
)
from eda.units import QuantityError, format_quantity, parse_quantity

NAME = "try"
DEFAULT_START = 0.1  # s, first input without design/inputs.yaml
DEFAULT_SETTLE = 0.1  # s, run on after the last input
DEFAULT_STEP = 1e-3  # s


@dataclass
class TryRequest:
    """The options of `eda try`, as typed."""

    keys: str | None = None
    at: str | None = None  # time of the first key (default: inputs.yaml start)
    events: list[str] = field(default_factory=list[str])  # "150ms SW1.state=pressed"
    sends: list[str] = field(default_factory=list[str])  # "300ms UART_RX rate 100\n"
    sets: list[str] = field(default_factory=list[str])  # "RV1.position=25%"
    from_test: str | None = None
    stop: str | None = None
    step: str | None = None
    measures: list[str] = field(default_factory=list[str])


class _OptionError(Exception):
    pass


def run(root: Path, request: TryRequest) -> Result:
    diag = Diagnostics()
    design = load_design(root, diag)
    data: dict[str, JsonValue] = {}
    if design is not None:
        try:
            case = _build(design, request)
        except _OptionError as exc:
            diag.error("invalid_option", str(exc), Location(file="command line"))
        else:
            data = _run(design, case, diag)
    result = diag.to_result("try", root.name, data)
    out = root / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "try.json").write_text(result.model_dump_json(indent=1), encoding="utf-8")
    return result


def _build(design: Design, request: TryRequest) -> TryTest:
    """The `try` test: the base test (or a plain tran run) plus the inputs."""
    inputs = design.inputs
    base: SimTest | None = None
    if request.from_test is not None:
        base = _raw_test(design, request.from_test)
        if not isinstance(base.analysis, TranAnalysis):
            raise _OptionError(f"--from {request.from_test}: only a tran test can take inputs")

    events: list[SimEvent] = list(base.events) if base else []
    end = max((_seconds(e.at, "event") for e in events), default=0.0)
    try:
        if request.keys:
            at = _seconds(request.at, "--at") if request.at else _start(design)
            event = SimEvent(at=format_quantity(at, "s"), keys=request.keys)
            events.append(event)
            if inputs is not None:
                hold, gap = key_timing(event, inputs)
                end = max(end, at + len(split_keys(request.keys)) * (hold + gap))
        for text in request.events:
            time, rest = _timed(text, "--event", "TIME PART.param=VALUE")
            ref, param, value = _assignment(rest, "--event")
            events.append(SimEvent(at=text.split()[0], part=ref, set={param: value}))
            end = max(end, time)
        for text in request.sends:
            time, rest = _timed(text, "--send", "TIME NET TEXT")
            net, _, payload = rest.partition(" ")
            if not net or not payload:
                raise _OptionError(f"--send {text!r}: give TIME NET TEXT, e.g. 300ms UART_RX hi\\n")
            events.append(SimEvent(at=text.split()[0], serial=net, send=_unescape(payload)))
            end = max(end, time)
    except (ValueError, QuantityError, ValidationError) as exc:
        raise _OptionError(str(exc)) from exc

    parts = {ref: dict(values) for ref, values in base.parts.items()} if base else {}
    for text in request.sets:
        ref, param, value = _assignment(text, "--set")
        parts.setdefault(ref, {})[param] = value

    settle = inputs.settle if inputs else DEFAULT_SETTLE
    base_stop, step = 0.0, request.step
    if base is not None and isinstance(base.analysis, TranAnalysis):
        base_stop = _seconds(base.analysis.stop, "the test's stop")
        step = step or base.analysis.step
    stop = _seconds(request.stop, "--stop") if request.stop else max(base_stop, end + settle)
    step = step or format_quantity(inputs.step if inputs else DEFAULT_STEP, "s")
    try:
        return TryTest(
            description="eda try",
            analysis=TranAnalysis(type="tran", step=step, stop=format_quantity(stop, "s")),
            firmware=base.firmware if base else design.firmware is not None,
            parts=parts,
            events=events,
            expect=[Expectation(measure=m) for m in request.measures],
        )
    except ValidationError as exc:
        raise _OptionError("; ".join(e["msg"] for e in exc.errors())) from exc


def _start(design: Design) -> float:
    return design.inputs.start if design.inputs else DEFAULT_START


def _run(design: Design, case: TryTest, diag: Diagnostics) -> dict[str, JsonValue]:
    src = SourceMap(file="command line", positions={})
    tests = compile_tests(
        {NAME: case}, src, design.circuit, design.firmware, design.inputs, diag, report_only=True
    )
    test = tests.get(NAME)
    if test is None or not diag.ok:
        return {}
    image = simulate.firmware_image(design, diag) if test.firmware else None
    if test.firmware and image is None:
        return {}
    outcome = simulate.run_test(design, test, design.paths.out_dir / NAME, diag, image)
    if outcome is None:
        return {}
    assert isinstance(test.analysis, Tran)
    outcome["stop"] = format_quantity(test.analysis.stop, "s")
    return {NAME: outcome}


def _raw_test(design: Design, name: str) -> SimTest:
    """A test of design/test.yaml as written (the design loaded, so it is valid)."""
    diag = Diagnostics()
    doc = load_yaml(design.paths.test_file, "design/test.yaml", diag)
    raw = SimTestFile.model_validate(doc.data) if doc is not None else None
    if raw is None or name not in raw.tests:
        known = ", ".join(design.tests) or "none"
        raise _OptionError(f"--from {name}: no such test (tests: {known})")
    return raw.tests[name]


def _seconds(text: str | None, what: str) -> float:
    try:
        return parse_quantity(text or "", "s")
    except QuantityError as exc:
        raise _OptionError(f"{what}: {exc}") from exc


def _timed(text: str, option: str, form: str) -> tuple[float, str]:
    time, _, rest = text.strip().partition(" ")
    if not rest.strip():
        raise _OptionError(f"{option} {text!r}: give {form}")
    return _seconds(time, option), rest.strip()


def _assignment(text: str, option: str) -> tuple[str, str, str]:
    """PART.param=VALUE"""
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)=(\S+)\s*", text)
    if match is None:
        raise _OptionError(f"{option} {text!r}: give PART.param=VALUE, e.g. SW1.state=pressed")
    return match[1], match[2], match[3]


def _unescape(text: str) -> str:
    """\\n, \\r, \\t, and \\\\ in UART text typed on a command line."""
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}
    return re.sub(r"\\([nrt\\])", lambda m: escapes[m[1]], text)
