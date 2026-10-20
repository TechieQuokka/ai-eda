"""Load a project and resolve it into checked, typed objects.

Pydantic checks structure; this module checks meaning: part types, parameter values and
units, pin references, and test expressions. ERC lives in `eda.verify.erc`.
"""

from pathlib import Path

from eda.loader import Document, SourceMap, load_yaml, validate
from eda.model import (
    GROUND,
    Ac,
    Analysis,
    CompiledExpectation,
    CompiledTest,
    Design,
    Op,
    PinKey,
    ResolvedCircuit,
    ResolvedPart,
    Tran,
    domain_of,
)
from eda.project import ProjectPaths, load_parts
from eda.result import Diagnostics, Location
from eda.schema.circuit import Circuit
from eda.schema.part import PartSpec
from eda.schema.project import Project
from eda.schema.testspec import AcAnalysis, Expectation, OpAnalysis, SimTestFile, TranAnalysis
from eda.units import QuantityError, format_quantity, parse_quantity
from eda.verify.erc import run_erc
from eda.verify.measure import (
    Measure,
    MeasureSyntaxError,
    MeasureTypeError,
    Probe,
    check,
    parse_measure,
    probes,
)


def load_design(root: Path, diag: Diagnostics) -> Design | None:
    """Load and check everything. Returns None if any error was recorded."""
    paths = ProjectPaths(root)
    project_doc = load_yaml(paths.project_file, paths.rel(paths.project_file), diag)
    project = validate(Project, project_doc, diag) if project_doc is not None else None
    parts = load_parts(diag, paths.parts_dir, paths.root)

    circuit: ResolvedCircuit | None = None
    circuit_doc = load_yaml(paths.circuit_file, paths.rel(paths.circuit_file), diag)
    if circuit_doc is not None:
        raw_circuit = validate(Circuit, circuit_doc, diag)
        if raw_circuit is not None:
            circuit = _resolve_circuit(raw_circuit, circuit_doc, parts, diag)
            run_erc(circuit, diag)

    tests: dict[str, CompiledTest] = {}
    test_doc = load_yaml(paths.test_file, paths.rel(paths.test_file), diag)
    if test_doc is not None:
        raw_tests = validate(SimTestFile, test_doc, diag)
        if raw_tests is not None:
            tests = _compile_tests(raw_tests, test_doc.source, circuit, diag)

    if not diag.ok or project is None or circuit is None:
        return None
    return Design(paths=paths, project=project, circuit=circuit, tests=tests, parts=parts)


# --- circuit -----------------------------------------------------------------------------


def _resolve_circuit(
    raw: Circuit, doc: Document, parts: dict[str, PartSpec], diag: Diagnostics
) -> ResolvedCircuit:
    src = doc.source
    _check_case_collisions("part reference", list(raw.parts), ("parts",), src, diag)
    _check_case_collisions("net name", list(raw.nets), ("nets",), src, diag)

    resolved: dict[str, ResolvedPart] = {}
    for ref, inst in raw.parts.items():
        spec = parts.get(inst.type)
        if spec is None:
            diag.error(
                "unknown_part_type",
                f"{ref}: unknown part type {inst.type!r}; available: {', '.join(sorted(parts))}. "
                "Vendor parts must be imported first: eda model import FILE --id ID",
                src.at("parts", ref, "type"),
                available=sorted(parts),
            )
            continue
        values, labels = _resolve_params(ref, spec, inst.params, src, diag)
        resolved[ref] = ResolvedPart(
            ref=ref,
            spec=spec,
            values=values,
            labels=labels,
            spice_name=_spice_name(spec.spice.prefix, ref),
        )

    nets: dict[str, tuple[PinKey, ...]] = {}
    seen: dict[PinKey, str] = {}
    for net, members in raw.nets.items():
        if net != GROUND and net.upper() == GROUND:
            diag.error(
                "ground_name",
                f"ground net must be spelled {GROUND!r}, not {net!r}",
                src.at("nets", net),
            )
        pins: list[PinKey] = []
        for index, member in enumerate(members):
            ref, pin = member.split(".", 1)
            at = src.at("nets", net, index)
            if ref not in raw.parts:
                diag.error("unknown_part", f"{member}: no part named {ref!r}", at)
                continue
            part = resolved.get(ref)
            if part is not None and pin not in part.spec.pins:
                diag.error(
                    "unknown_pin",
                    f"{member}: {part.spec.id} has pins {part.spec.pins}, not {pin!r}",
                    at,
                    pins=list(part.spec.pins),
                )
                continue
            key = (ref, pin)
            if key in seen:
                other = seen[key]
                message = (
                    f"{member} is listed twice in net {net!r}"
                    if other == net
                    else f"{member} is in both {other!r} and {net!r} (short between nets)"
                )
                diag.error("pin_in_multiple_nets", message, at, nets=sorted({other, net}))
                continue
            seen[key] = net
            pins.append(key)
        nets[net] = tuple(pins)
    return ResolvedCircuit(parts=resolved, nets=nets, source=src)


def _resolve_params(
    ref: str,
    spec: PartSpec,
    given: dict[str, object],
    src: SourceMap,
    diag: Diagnostics,
) -> tuple[dict[str, float | str], dict[str, str]]:
    values: dict[str, float | str] = {}
    labels: dict[str, str] = {}
    for name in given:
        if name not in spec.params:
            known = ", ".join(spec.params) or "none"
            diag.error(
                "unknown_param",
                f"{ref}: {spec.id} has no parameter {name!r} (parameters: {known})",
                src.at("parts", ref, name),
            )
    for name, param in spec.params.items():
        at = src.at("parts", ref, name)
        raw = given.get(name, param.default)
        if raw is None:
            diag.error(
                "missing_param",
                f"{ref}: {spec.id} requires {name!r}"
                + (f" in {param.unit}" if param.unit else f", one of {param.choices}"),
                src.at("parts", ref),
            )
            continue
        if param.unit is not None:
            try:
                value = parse_quantity(raw, param.unit)
            except QuantityError as exc:
                diag.error("invalid_value", f"{ref}.{name}: {exc}", at)
                continue
            values[name] = value
            labels[name] = format_quantity(value, param.unit)
        else:
            choices = param.choices or []
            if not isinstance(raw, str) or raw not in choices:
                diag.error(
                    "invalid_choice",
                    f"{ref}.{name}: {raw!r} is not one of {choices}",
                    at,
                    choices=choices,
                )
                continue
            values[name] = raw
            labels[name] = raw
    return values, labels


def _spice_name(prefix: str, ref: str) -> str:
    """SPICE decides the element kind by the first letter, so keep it correct."""
    return ref if ref[0].upper() == prefix else f"{prefix}_{ref}"


def _check_case_collisions(
    what: str, names: list[str], base: tuple[str], src: SourceMap, diag: Diagnostics
) -> None:
    first: dict[str, str] = {}
    for name in names:
        key = name.lower()
        if key in first:
            diag.error(
                "case_collision",
                f"{what} {name!r} differs from {first[key]!r} only by case (SPICE ignores case)",
                src.at(*base, name),
            )
        else:
            first[key] = name


# --- tests -------------------------------------------------------------------------------


def _compile_tests(
    raw: SimTestFile,
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    diag: Diagnostics,
) -> dict[str, CompiledTest]:
    compiled: dict[str, CompiledTest] = {}
    for name, case in raw.tests.items():
        analysis = _compile_analysis(name, case.analysis, src, diag)
        if isinstance(analysis, Ac) and circuit is not None and not _has_ac_source(circuit):
            diag.error(
                "ac_no_source",
                f"{name}: ac analysis needs a source with an `ac` magnitude, "
                "e.g. { type: sine_voltage_source, ..., ac: 1V }",
                _at(src, name),
            )
        expectations: list[CompiledExpectation] = []
        for index, exp in enumerate(case.expect):
            item = _compile_expectation(name, index, exp, analysis, src, circuit, diag)
            if item is not None:
                expectations.append(item)
        if analysis is not None and len(expectations) == len(case.expect):
            compiled[name] = CompiledTest(
                name=name,
                description=case.description,
                analysis=analysis,
                expectations=tuple(expectations),
                location=src.at("tests", name),
            )
    return compiled


def _has_ac_source(circuit: ResolvedCircuit) -> bool:
    return any(part.values.get("ac", 0.0) != 0.0 for part in circuit.parts.values())


def _compile_analysis(
    name: str, raw: OpAnalysis | TranAnalysis | AcAnalysis, src: SourceMap, diag: Diagnostics
) -> Analysis | None:
    if isinstance(raw, OpAnalysis):
        return Op()
    unit = "s" if isinstance(raw, TranAnalysis) else "Hz"
    fields = ("step", "stop", "start") if isinstance(raw, TranAnalysis) else ("start", "stop")
    values: dict[str, float | None] = {}
    for field in fields:
        text = getattr(raw, field)
        if text is None:
            values[field] = None
            continue
        try:
            values[field] = parse_quantity(text, unit)
        except QuantityError as exc:
            diag.error("invalid_value", f"{name}.analysis.{field}: {exc}", _at(src, name, field))
            return None
    if isinstance(raw, AcAnalysis):
        start, stop = values["start"], values["stop"]
        assert start is not None and stop is not None
        if not 0 < start < stop:
            diag.error(
                "invalid_analysis", f"{name}: ac needs 0 < start < stop", _at(src, name, "start")
            )
            return None
        return Ac(start=start, stop=stop, points_per_decade=raw.points_per_decade)
    step, stop, start = values["step"], values["stop"], values["start"]
    assert step is not None and stop is not None
    if step <= 0 or stop <= 0 or step >= stop:
        diag.error(
            "invalid_analysis",
            f"{name}: tran needs 0 < step < stop",
            _at(src, name, "step"),
        )
        return None
    if start is not None and not 0 <= start < stop:
        diag.error("invalid_analysis", f"{name}: tran needs 0 <= start < stop", _at(src, name))
        return None
    return Tran(step=step, stop=stop, start=start)


def _at(src: SourceMap, name: str, field: str | None = None) -> Location:
    return src.at("tests", name, "analysis", field) if field else src.at("tests", name, "analysis")


def _compile_expectation(
    name: str,
    index: int,
    raw: Expectation,
    analysis: Analysis | None,
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    diag: Diagnostics,
) -> CompiledExpectation | None:
    base = ("tests", name, "expect", index)
    at = src.at(*base)
    at_measure = src.at(*base, "measure")
    try:
        node = parse_measure(raw.measure)
    except MeasureSyntaxError as exc:
        diag.error("invalid_measure", str(exc), at_measure)
        return None

    ok = True
    measure: Measure | None = None
    if analysis is not None:
        try:
            measure = check(node, domain_of(analysis))
        except MeasureTypeError as exc:
            rule = "missing_reduction" if "reduce it" in str(exc) else "invalid_measure"
            diag.error(rule, str(exc), at_measure)
            ok = False
    if circuit is not None:
        for probe in probes(node):
            message = _probe_problem(probe, circuit)
            if message is not None:
                rule, text = message
                diag.error(rule, f"{raw.measure}: {text}", at_measure)
                ok = False

    if raw.min is None and raw.max is None:
        diag.error("missing_bound", f"{raw.measure}: give min, max, or both", at)
        return None
    if measure is None:
        return None
    bounds: dict[str, float | None] = {"min": None, "max": None}
    for key in ("min", "max"):
        text = getattr(raw, key)
        if text is None:
            continue
        try:
            bounds[key] = parse_quantity(text, measure.unit)
        except QuantityError as exc:
            diag.error("invalid_value", f"{key}: {exc}", src.at(*base, key))
            ok = False
    low, high = bounds["min"], bounds["max"]
    if low is not None and high is not None and low > high:
        diag.error("invalid_bounds", f"{raw.measure}: min is greater than max", at)
        ok = False
    if not ok:
        return None
    return CompiledExpectation(index=index, measure=measure, min=low, max=high, location=at)


def _probe_problem(probe: Probe, circuit: ResolvedCircuit) -> tuple[str, str] | None:
    if probe.kind == "voltage":
        for net in probe.targets:
            if net not in circuit.nets:
                return "unknown_net", f"no net named {net!r}; nets: {', '.join(circuit.nets)}"
        return None
    ref = probe.targets[0]
    part = circuit.parts.get(ref)
    if part is None:
        return "unknown_part", f"no part named {ref!r}"
    spice = part.spec.spice
    if probe.pin is None and spice.pin_currents:
        pins = ", ".join(f"current({ref}.{p})" for p in part.spec.pins)
        return "pin_current_required", f"{ref} has no single current; measure a pin: {pins}"
    if probe.pin is not None:
        if not spice.pin_currents:
            return "invalid_measure", f"{ref} is a two-terminal part; use current({ref})"
        if probe.pin not in part.spec.pins:
            return "unknown_pin", f"{ref} has pins {part.spec.pins}, not {probe.pin!r}"
    return None
