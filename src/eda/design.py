"""Load a project and resolve it into checked, typed objects.

Pydantic checks structure; this module checks meaning: part types, parameter values and
units, pin references, and test expressions. ERC lives in `eda.verify.erc`.
"""

import math
import re
from dataclasses import replace
from pathlib import Path

from eda import fab as fabs
from eda import firmware as fw
from eda.footprints import pad_numbers
from eda.loader import Document, SourceMap, load_yaml, validate
from eda.model import (
    GROUND,
    Ac,
    Analysis,
    BoardPart,
    BoardPlacement,
    BoardPour,
    BoardRules,
    CompiledEvent,
    CompiledExpectation,
    CompiledTest,
    Dc,
    Design,
    DisplayExpectation,
    Op,
    PinKey,
    ResolvedBoard,
    ResolvedCircuit,
    ResolvedFirmware,
    ResolvedPart,
    SerialExpectation,
    Tran,
    domain_of,
    x_unit_of,
)
from eda.netlist.spice import alter_commands
from eda.project import ProjectPaths, load_parts
from eda.result import Diagnostics, Location
from eda.schema.board import BoardFile, FabSpec
from eda.schema.circuit import Circuit
from eda.schema.common import Scalar
from eda.schema.firmware import FirmwareFile
from eda.schema.part import ParamSpec, PartSpec, SegmentDisplaySpec, Ssd1306DisplaySpec
from eda.schema.project import Project
from eda.schema.testspec import (
    AcAnalysis,
    DcAnalysis,
    Expectation,
    OpAnalysis,
    SimEvent,
    SimTest,
    SimTestFile,
    TranAnalysis,
)
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

# Upper bounds that keep a run from hanging or filling the disk: a mistyped step (1ns for
# 10 s) asks for billions of points, which ngspice would try to compute and store.
MAX_TRAN_POINTS = 1_000_000
MAX_AC_POINTS = 100_000
MAX_DC_POINTS = 10_000  # one operating point each
MAX_COSIM_WINDOWS = 100_000  # firmware co-simulation exchanges values once per step
MAX_BOARD_MM = 1000.0  # no board dimension, rule, or placement coordinate goes beyond 1 m


def load_design(root: Path, diag: Diagnostics) -> Design | None:
    """Load and check everything. Returns None if any error was recorded."""
    paths = ProjectPaths(root)
    project_doc = load_yaml(paths.project_file, paths.rel(paths.project_file), diag)
    project = validate(Project, project_doc, diag) if project_doc is not None else None
    parts = load_parts(diag, paths.parts_dir, paths.root)

    circuit: ResolvedCircuit | None = None
    raw_circuit: Circuit | None = None
    circuit_doc = load_yaml(paths.circuit_file, paths.rel(paths.circuit_file), diag)
    if circuit_doc is not None:
        raw_circuit = validate(Circuit, circuit_doc, diag)
        if raw_circuit is not None:
            circuit = _resolve_circuit(raw_circuit, circuit_doc, parts, diag)
            run_erc(circuit, diag)

    firmware: ResolvedFirmware | None = None
    if paths.firmware_file.is_file() and circuit is not None:
        fw_doc = load_yaml(paths.firmware_file, paths.rel(paths.firmware_file), diag)
        raw_fw = validate(FirmwareFile, fw_doc, diag) if fw_doc is not None else None
        if raw_fw is not None and fw_doc is not None:
            firmware = fw.resolve(raw_fw, fw_doc.source, circuit, diag)

    tests: dict[str, CompiledTest] = {}
    test_doc = load_yaml(paths.test_file, paths.rel(paths.test_file), diag)
    if test_doc is not None:
        raw_tests = validate(SimTestFile, test_doc, diag)
        if raw_tests is not None:
            tests = _compile_tests(raw_tests, test_doc.source, circuit, firmware, diag)

    board: ResolvedBoard | None = None
    if paths.board_file.is_file() and circuit is not None and circuit_doc is not None:
        board_doc = load_yaml(paths.board_file, paths.rel(paths.board_file), diag)
        raw_board = validate(BoardFile, board_doc, diag) if board_doc is not None else None
        if raw_board is not None and board_doc is not None and raw_circuit is not None:
            board = _resolve_board(raw_board, board_doc.source, raw_circuit, circuit, diag)

    if not diag.ok or project is None or circuit is None:
        return None
    return Design(
        paths=paths,
        project=project,
        circuit=circuit,
        tests=tests,
        parts=parts,
        board=board,
        firmware=firmware,
    )


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
            mpn=inst.mpn,
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
        parsed = _param_value(ref, name, param, raw, at, diag)
        if parsed is not None:
            values[name], labels[name] = parsed
    return values, labels


def _param_value(
    ref: str, name: str, param: ParamSpec, raw: object, at: Location, diag: Diagnostics
) -> tuple[float | str, str] | None:
    """(value, label) of one parameter: an SI float for quantities, the choice string."""
    if param.unit is not None:
        try:
            value = parse_quantity(raw, param.unit)
        except QuantityError as exc:
            diag.error("invalid_value", f"{ref}.{name}: {exc}", at)
            return None
        problem = param.out_of_range(value)
        if problem is not None:
            shown = format_quantity(value, param.unit)
            diag.error("invalid_value", f"{ref}.{name} = {shown} {problem}", at)
            return None
        return value, format_quantity(value, param.unit)
    choices = param.choices or []
    if not isinstance(raw, str) or raw not in choices:
        diag.error(
            "invalid_choice",
            f"{ref}.{name}: {raw!r} is not one of {choices}",
            at,
            choices=choices,
        )
        return None
    return raw, raw


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


# --- board -------------------------------------------------------------------------------


def _mm(text: str, where: Location, label: str, diag: Diagnostics) -> float | None:
    try:
        return parse_quantity(text, "m") * 1000.0
    except QuantityError as exc:
        diag.error("invalid_value", f"{label}: {exc}", where)
        return None


def _pad_list(value: str | int | list[str]) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _pad_order(pad: str) -> tuple[int, str]:
    return (int(pad), "") if pad.isdigit() else (1 << 30, pad)


def _resolve_board(
    raw: BoardFile,
    src: SourceMap,
    raw_circuit: Circuit,
    circuit: ResolvedCircuit,
    diag: Diagnostics,
) -> ResolvedBoard | None:
    csrc = circuit.source
    ok = True

    def mm(text: str | None, *path: str) -> float | None:
        nonlocal ok
        if text is None:
            return None
        value = _mm(text, src.at(*path), ".".join(path), diag)
        if value is None:
            ok = False
        elif value <= 0 and path[-1] not in ("x", "y"):
            diag.error("invalid_value", f"{'.'.join(path)} must be positive", src.at(*path))
            ok = False
            return None
        elif abs(value) > MAX_BOARD_MM:
            diag.error(
                "invalid_value",
                f"{'.'.join(path)} = {text} is beyond {MAX_BOARD_MM:g} mm",
                src.at(*path),
            )
            ok = False
            return None
        return value

    width = mm(raw.board.width, "board", "width")
    height = mm(raw.board.height, "board", "height")
    if (raw.board.width is None) != (raw.board.height is None):
        diag.error(
            "invalid_board", "give both board.width and board.height, or neither", src.at("board")
        )
        ok = False
    margin = mm(raw.board.margin, "board", "margin")
    spacing = mm(raw.board.spacing, "board", "spacing")
    rules = [
        mm(getattr(raw.rules, f), "rules", f)
        for f in ("clearance", "track_width", "via_diameter", "via_drill")
    ]
    via_diameter, via_drill = rules[2], rules[3]
    if via_diameter is not None and via_drill is not None and via_drill >= via_diameter:
        diag.error(
            "invalid_value",
            f"rules.via_drill ({raw.rules.via_drill}) must be smaller than "
            f"rules.via_diameter ({raw.rules.via_diameter}), or the via has no copper ring",
            src.at("rules", "via_drill"),
        )
        ok = False

    placements: dict[str, BoardPlacement] = {}
    for ref, place in raw.placement.items():
        if ref not in circuit.parts:
            diag.error(
                "unknown_part",
                f"placement for {ref!r}, which is not in the circuit",
                src.at("placement", ref),
            )
            ok = False
            continue
        x = mm(place.x, "placement", ref, "x")
        y = mm(place.y, "placement", ref, "y")
        try:
            rotation = parse_quantity(place.rotation, "deg")
        except QuantityError as exc:
            diag.error(
                "invalid_value",
                f"placement.{ref}.rotation: {exc}",
                src.at("placement", ref, "rotation"),
            )
            ok = False
            continue
        if x is None or y is None:
            continue
        if width is not None and height is not None and not (0 <= x <= width and 0 <= y <= height):
            diag.error(
                "placement_outside",
                f"placement.{ref} at ({place.x}, {place.y}) is outside the "
                f"{raw.board.width} x {raw.board.height} board (corner at 0, 0)",
                src.at("placement", ref),
            )
            ok = False
            continue
        placements[ref] = BoardPlacement(x, y, rotation, place.side)

    parts: list[BoardPart] = []
    for ref, part in circuit.parts.items():
        inst = raw_circuit.parts[ref]
        default = part.spec.pcb
        footprint = inst.footprint or (default.footprint if default else None)
        pads: dict[str, list[str]] | None = (
            {pin: _pad_list(pad) for pin, pad in inst.pads.items()}
            if inst.pads is not None
            else ({pin: _pad_list(p) for pin, p in default.pads.items()} if default else None)
        )
        at = csrc.at("parts", ref)
        if footprint is None or pads is None:
            diag.error(
                "no_footprint",
                f"{ref}: {part.spec.id} has no default footprint; set `footprint:` "
                "(KiCad Library:Name) and `pads:` (pin → pad number) on the part",
                at,
            )
            ok = False
            continue
        available = pad_numbers(footprint)
        if available is None:
            diag.error(
                "unknown_footprint",
                f"{ref}: footprint {footprint!r} not found in the KiCad libraries",
                csrc.at("parts", ref, "footprint") if inst.footprint else at,
            )
            ok = False
            continue
        missing = [pin for pin in part.spec.pins if pin not in pads]
        bad = sorted({p for group in pads.values() for p in group if p not in available})
        extra = [pin for pin in pads if pin not in part.spec.pins]
        if missing or bad or extra:
            problems: list[str] = []
            if missing:
                problems.append(f"no pad for pin(s) {', '.join(missing)}")
            if extra:
                problems.append(f"unknown pin(s) {', '.join(extra)}")
            if bad:
                problems.append(f"pad(s) {', '.join(bad)} not in {footprint}")
            diag.error(
                "invalid_pads",
                f"{ref}: " + "; ".join(problems),
                at,
                footprint_pads=sorted(available),
            )
            ok = False
            continue
        owners: dict[str, list[str]] = {}
        for pin, group in pads.items():
            for pad in group:
                owners.setdefault(pad, []).append(pin)
        shared = {
            pad: pins
            for pad, pins in owners.items()
            if len({circuit.net_of(ref, pin) for pin in pins}) > 1
        }
        if shared:
            diag.error(
                "invalid_pads",
                f"{ref}: "
                + "; ".join(
                    f"pad {pad} is given to pins {', '.join(pins)} on different nets"
                    for pad, pins in sorted(shared.items())
                ),
                csrc.at("parts", ref, "pads") if inst.pads is not None else at,
            )
            ok = False
            continue
        intended = set(default.nc) if default and inst.footprint is None else set[str]()
        used = {p for group in pads.values() for p in group}
        optional = set(part.spec.optional_pins)
        unused = sorted(available - used - intended, key=_pad_order)
        if unused and part.spec.category not in ("passive", "source") and not optional:
            diag.warning(
                "unused_pads",
                f"{ref}: pads {', '.join(unused)} of {footprint} are not connected; "
                "check the datasheet (e.g. tie unused amplifier channels off)",
                at,
                pads=unused,
            )
        pad_nets = {
            pad: net
            for pin in part.spec.pins
            if (net := circuit.net_of(ref, pin)) is not None
            for pad in pads[pin]
        }
        value = part.label(default.value if default else None) or part.spec.id
        if part.spec.generic and part.mpn:
            value = part.mpn  # the chip that will be soldered, not the model's figures
        parts.append(BoardPart(ref, value, footprint, pad_nets, placements.get(ref)))

    board_nets = {net for part in parts for net in part.pads.values()}
    pours: list[BoardPour] = []
    used: set[str] = set()  # layers that already have a pour
    for index, pour in enumerate(raw.pours):
        at = src.at("pours", index)
        if pour.net not in board_nets:
            diag.error(
                "pour_net",
                f"pours[{index}]: net {pour.net!r} has no pads on the board; nets: "
                + ", ".join(sorted(board_nets)),
                src.at("pours", index, "net"),
            )
            ok = False
            continue
        layers = ("top", "bottom") if pour.layer == "both" else (pour.layer,)
        clash = [layer for layer in layers if layer in used]
        if clash:
            diag.error(
                "pour_overlap",
                f"pours[{index}]: the {clash[0]} layer already has a pour; one pour per layer",
                at,
            )
            ok = False
            continue
        used.update(layers)
        pour_clearance = _mm(pour.clearance, src.at("pours", index, "clearance"), "clearance", diag)
        min_width = _mm(pour.min_width, src.at("pours", index, "min_width"), "min_width", diag)
        if pour_clearance is None or min_width is None:
            ok = False
            continue
        pours.append(BoardPour(pour.net, layers, pour_clearance, min_width))

    if not ok or margin is None or spacing is None or any(r is None for r in rules):
        return None
    clearance, track, via_d, via_drill = (float(r or 0) for r in rules)
    fab = _resolve_fab(raw.fab, src, diag)
    if raw.fab is not None and fab is None:
        return None
    if fab is not None:
        checks = [
            ("rules.track_width", track, "min_track", src.at("rules", "track_width")),
            ("rules.clearance", clearance, "min_clearance", src.at("rules", "clearance")),
            ("rules.via_drill", via_drill, "min_drill", src.at("rules", "via_drill")),
            (
                "the via ring",
                (via_d - via_drill) / 2,
                "min_via_ring",
                src.at("rules", "via_diameter"),
            ),
        ]
        for index, pour in enumerate(pours):
            checks.append(
                (
                    f"pours[{index}].clearance",
                    pour.clearance,
                    "min_clearance",
                    src.at("pours", index),
                )
            )
            checks.append(
                (f"pours[{index}].min_width", pour.min_width, "min_track", src.at("pours", index))
            )
        failed = [
            fabs.below(fab, diag, what, value, limit, at) for what, value, limit, at in checks
        ]
        if width is not None and height is not None:
            before = len(diag.errors)
            fabs.check_size(fab, width, height, diag, src.at("board"))
            failed.append(len(diag.errors) > before)
        if any(failed):
            return None
    return ResolvedBoard(
        width=width,
        height=height,
        margin=margin,
        spacing=spacing,
        layers=raw.board.layers,
        rules=BoardRules(clearance, track, via_d, via_drill),
        parts=tuple(parts),
        passes=raw.routing.passes,
        source=src,
        pours=tuple(pours),
        placer=raw.board.placer,
        fab=fab,
    )


def _resolve_fab(raw: str | FabSpec | None, src: SourceMap, diag: Diagnostics) -> fabs.Fab | None:
    if raw is None:
        return None
    spec = FabSpec(base=raw) if isinstance(raw, str) else raw
    at = src.at("fab")
    base: fabs.Fab | None = None
    if spec.base is not None:
        base = fabs.PRESETS.get(spec.base)
        if base is None:
            diag.error(
                "unknown_fab",
                f"no PCB house preset {spec.base!r}; presets: {', '.join(sorted(fabs.PRESETS))}, "
                "or give the limits yourself (fab: { min_track: 0.15mm, ... })",
                at if isinstance(raw, str) else src.at("fab", "base"),
            )
            return None
    values: dict[str, float] = {}
    for field in fabs.FIELDS:
        text: str | None = getattr(spec, field)
        if text is None:
            fallback = MAX_BOARD_MM if field.startswith("max_") else 0.0
            values[field] = float(getattr(base, field)) if base is not None else fallback
            continue
        value = _mm(text, src.at("fab", field), f"fab.{field}", diag)
        if value is None:
            return None
        if value < 0:
            diag.error("invalid_value", f"fab.{field} cannot be negative", src.at("fab", field))
            return None
        values[field] = value
    name = spec.name or (base.name if base is not None else "the PCB house")
    return fabs.Fab(name=name, **values)


# --- tests -------------------------------------------------------------------------------


def _compile_tests(
    raw: SimTestFile,
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    firmware: ResolvedFirmware | None,
    diag: Diagnostics,
) -> dict[str, CompiledTest]:
    compiled: dict[str, CompiledTest] = {}
    if not raw.tests:
        diag.warning(
            "no_tests",
            "design/test.yaml has no tests, so nothing checks the design; add one per requirement",
            src.at("tests"),
        )
    for name, case in raw.tests.items():
        analysis = _compile_analysis(name, case.analysis, src, circuit, diag)
        for net in case.pins:
            if firmware is None or net not in firmware.signals:
                declared = ", ".join(firmware.signals) if firmware else "none loaded"
                diag.error(
                    "unknown_signal",
                    f"{name}.pins.{net}: not a firmware signal; signals: {declared}",
                    src.at("tests", name, "pins", net),
                )
        if isinstance(analysis, Ac) and circuit is not None and not _has_ac_source(circuit):
            diag.error(
                "ac_no_source",
                f"{name}: ac analysis needs a source with an `ac` magnitude, "
                "e.g. { type: sine_voltage_source, ..., ac: 1V }",
                _at(src, name),
            )
        ok = True
        if case.firmware:
            ok = _check_firmware_test(name, case, analysis, src, firmware, diag)
        overrides = _compile_overrides(name, case, src, circuit, diag)
        events = _compile_events(name, case, analysis, src, circuit, firmware, diag)
        ok = ok and overrides is not None and events is not None
        expectations: list[CompiledExpectation] = []
        serial: list[SerialExpectation] = []
        displays: list[DisplayExpectation] = []
        for index, exp in enumerate(case.expect):
            if exp.serial is not None:
                found = _compile_serial(name, index, exp, case.firmware, src, firmware, diag)
                if found is not None:
                    serial.append(found)
                continue
            if exp.display is not None:
                shown = _compile_display(
                    name, index, exp, case.firmware, analysis, src, circuit, diag
                )
                if shown is not None:
                    displays.append(shown)
                continue
            item = _compile_expectation(name, index, exp, analysis, src, circuit, diag)
            if item is not None:
                expectations.append(item)
        judged = len(expectations) + len(serial) + len(displays)
        if ok and analysis is not None and judged == len(case.expect):
            compiled[name] = CompiledTest(
                name=name,
                description=case.description,
                analysis=analysis,
                expectations=tuple(expectations),
                location=src.at("tests", name),
                pins=dict(case.pins),
                firmware=case.firmware,
                serial=tuple(serial),
                displays=tuple(displays),
                parts=overrides or {},
                events=tuple(events or ()),
            )
    return compiled


def _compile_events(
    name: str,
    case: SimTest,
    analysis: Analysis | None,
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    firmware: ResolvedFirmware | None,
    diag: Diagnostics,
) -> list[CompiledEvent] | None:
    """`events:` of a tran test: part parameter changes and UART input, sorted by time."""
    if not case.events:
        return []
    if analysis is not None and not isinstance(analysis, Tran):
        diag.error(
            "events_need_tran",
            f"{name}: events need a tran analysis",
            src.at("tests", name, "events"),
        )
        return None
    found: list[CompiledEvent] = []
    ok = True
    for index, raw in enumerate(case.events):
        base = ("tests", name, "events", index)
        try:
            time = parse_quantity(raw.at, "s")
        except QuantityError as exc:
            diag.error("invalid_value", f"at: {exc}", src.at(*base, "at"))
            ok = False
            continue
        if isinstance(analysis, Tran) and not 0 <= time < analysis.stop:
            diag.error(
                "event_time", f"{name}: event at {raw.at} is outside the run", src.at(*base, "at")
            )
            ok = False
            continue
        if raw.part is not None:
            event = _part_event(name, raw.part, raw.set or {}, time, base, src, circuit, diag)
        else:
            event = _serial_event(name, raw, time, case.firmware, base, src, firmware, diag)
        if event is None:
            ok = False
        else:
            found.append(event)
    found.sort(key=lambda e: e.time)
    return found if ok else None


def _part_event(
    name: str,
    ref: str,
    params: dict[str, Scalar],
    time: float,
    base: tuple[str | int, ...],
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    diag: Diagnostics,
) -> CompiledEvent | None:
    if circuit is None:
        return None
    part = circuit.parts.get(ref)
    if part is None:
        diag.error("unknown_part", f"{name}.events: no part named {ref!r}", src.at(*base, "part"))
        return None
    values: dict[str, tuple[float | str, str]] = {}
    for param_name, raw in params.items():
        param = part.spec.params.get(param_name)
        where = src.at(*base, "set", param_name)
        if param is None:
            known = ", ".join(part.spec.params) or "none"
            diag.error(
                "unknown_param",
                f"{ref}: {part.spec.id} has no parameter {param_name!r} (parameters: {known})",
                where,
            )
            return None
        parsed = _param_value(ref, param_name, param, raw, where, diag)
        if parsed is None:
            return None
        values[param_name] = parsed
    changed = replace(part, values={**part.values, **{k: v for k, (v, _) in values.items()}})
    try:
        alter_commands(part, changed, circuit)
    except ValueError as exc:
        diag.error("event_unsupported", f"{ref}: {exc}", src.at(*base, "set"))
        return None
    return CompiledEvent(time=time, location=src.at(*base), part=ref, values=values)


def _serial_event(
    name: str,
    raw: SimEvent,
    time: float,
    runs_firmware: bool,
    base: tuple[str | int, ...],
    src: SourceMap,
    firmware: ResolvedFirmware | None,
    diag: Diagnostics,
) -> CompiledEvent | None:
    assert raw.serial is not None and raw.send is not None
    if not runs_firmware:
        diag.error(
            "serial_needs_firmware", f"{name}: serial events need firmware: true", src.at(*base)
        )
        return None
    signals = firmware.signals if firmware else {}
    signal = signals.get(raw.serial)
    rx = re.fullmatch(r"UART(\d)_RX", (signal.function if signal else None) or "")
    if rx is None:
        nets = [s.net for s in signals.values() if (s.function or "").endswith("_RX")]
        diag.error(
            "serial_net",
            f"{raw.serial} is not a UART RX signal in firmware.yaml; UART RX nets: "
            + (", ".join(nets) or "none"),
            src.at(*base, "serial"),
        )
        return None
    return CompiledEvent(
        time=time, location=src.at(*base), net=raw.serial, uart=int(rx.group(1)), text=raw.send
    )


def _compile_overrides(
    name: str,
    case: SimTest,
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    diag: Diagnostics,
) -> dict[str, dict[str, tuple[float | str, str]]] | None:
    """Per-test part parameters, e.g. `parts: { SW1: { state: pressed } }`."""
    found: dict[str, dict[str, tuple[float | str, str]]] = {}
    ok = True
    for ref, params in case.parts.items():
        at = src.at("tests", name, "parts", ref)
        part = circuit.parts.get(ref) if circuit else None
        if part is None:
            if circuit is not None:
                diag.error("unknown_part", f"{name}.parts: no part named {ref!r}", at)
            ok = False
            continue
        found[ref] = {}
        for param_name, raw in params.items():
            param = part.spec.params.get(param_name)
            where = src.at("tests", name, "parts", ref, param_name)
            if param is None:
                known = ", ".join(part.spec.params) or "none"
                diag.error(
                    "unknown_param",
                    f"{ref}: {part.spec.id} has no parameter {param_name!r} (parameters: {known})",
                    where,
                )
                ok = False
                continue
            parsed = _param_value(ref, param_name, param, raw, where, diag)
            if parsed is None:
                ok = False
                continue
            found[ref][param_name] = parsed
    return found if ok else None


def _check_firmware_test(
    name: str,
    case: SimTest,
    analysis: Analysis | None,
    src: SourceMap,
    firmware: ResolvedFirmware | None,
    diag: Diagnostics,
) -> bool:
    at = src.at("tests", name, "firmware")
    if firmware is None:
        diag.error("no_firmware", f"{name}: firmware: true needs design/firmware.yaml", at)
        return False
    if analysis is not None and not isinstance(analysis, Tran):
        diag.error("firmware_needs_tran", f"{name}: firmware runs only in a tran analysis", at)
        return False
    if isinstance(analysis, Tran):
        windows = (analysis.stop - (analysis.start or 0.0)) / analysis.step
        if windows > MAX_COSIM_WINDOWS:
            diag.error(
                "too_many_points",
                f"{name}: firmware co-simulation would take {windows:.3g} steps "
                f"(limit {MAX_COSIM_WINDOWS}, about 1 ms each); raise step or shorten stop",
                src.at("tests", name, "analysis", "step"),
            )
            return False
    if case.pins:
        diag.error(
            "firmware_pins",
            f"{name}: pins overrides do not apply when the firmware drives the pins",
            src.at("tests", name, "pins"),
        )
        return False
    return True


def _compile_serial(
    name: str,
    index: int,
    raw: Expectation,
    runs_firmware: bool,
    src: SourceMap,
    firmware: ResolvedFirmware | None,
    diag: Diagnostics,
) -> SerialExpectation | None:
    base = ("tests", name, "expect", index)
    at = src.at(*base)
    assert raw.serial is not None and raw.match is not None
    if not runs_firmware:
        diag.error("serial_needs_firmware", f"{name}: serial expectations need firmware: true", at)
        return None
    signals = firmware.signals if firmware else {}
    signal = signals.get(raw.serial)
    tx = re.fullmatch(r"UART(\d)_TX", (signal.function if signal else None) or "")
    if tx is None:
        uarts = [s.net for s in signals.values() if (s.function or "").endswith("_TX")]
        diag.error(
            "serial_net",
            f"{raw.serial} is not a UART TX signal in firmware.yaml; UART TX nets: "
            + (", ".join(uarts) or "none"),
            src.at(*base, "serial"),
        )
        return None
    if raw.min is None and raw.max is None:
        diag.error("missing_bound", f"serial({raw.serial}): give min, max, or both", at)
        return None
    bounds = _match_bounds(raw, base, src, diag)
    if bounds is None:
        return None
    return SerialExpectation(
        index=index,
        net=raw.serial,
        uart=int(tx.group(1)),
        pattern=raw.match,
        min=bounds[0],
        max=bounds[1],
        location=at,
    )


def _match_bounds(
    raw: Expectation, base: tuple[str | int, ...], src: SourceMap, diag: Diagnostics
) -> tuple[float | None, float | None] | None:
    """Checks `match` (a regular expression with at most one group) and returns the numeric
    (min, max) of a serial or display expectation."""
    try:
        pattern = re.compile(raw.match or "")
    except re.error as exc:
        diag.error("invalid_match", f"match: {exc}", src.at(*base, "match"))
        return None
    if pattern.groups > 1:
        diag.error("invalid_match", "match: use at most one capture group", src.at(*base, "match"))
        return None
    bounds: dict[str, float | None] = {"min": None, "max": None}
    for key in ("min", "max"):
        value = getattr(raw, key)
        if value is None:
            continue
        try:
            bounds[key] = float(value)
        except ValueError:
            diag.error(
                "invalid_value", f"{key}: expected a number, got {value!r}", src.at(*base, key)
            )
            return None
    return bounds["min"], bounds["max"]


def _compile_display(
    name: str,
    index: int,
    raw: Expectation,
    runs_firmware: bool,
    analysis: Analysis | None,
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    diag: Diagnostics,
) -> DisplayExpectation | None:
    base = ("tests", name, "expect", index)
    at = src.at(*base)
    assert raw.display is not None
    if circuit is None or analysis is None:
        return None
    part = circuit.parts.get(raw.display)
    if part is None or part.spec.display is None:
        shown = [ref for ref, p in circuit.parts.items() if p.spec.display is not None]
        what = "no part named" if part is None else "not a display:"
        diag.error(
            "display_part",
            f"{name}: {what} {raw.display!r}; display parts: {', '.join(shown) or 'none'}",
            src.at(*base, "display"),
        )
        return None
    if not isinstance(analysis, (Op, Tran)):
        diag.error(
            "display_analysis",
            f"{name}: display({raw.display}) is read from an op or tran test",
            at,
        )
        return None
    when: float | None = None
    if raw.at is not None:
        if not isinstance(analysis, Tran):
            diag.error("display_analysis", f"{name}: `at` needs a tran test", src.at(*base, "at"))
            return None
        try:
            when = parse_quantity(raw.at, "s")
        except QuantityError as exc:
            diag.error("invalid_value", f"at: {exc}", src.at(*base, "at"))
            return None
        if not 0 < when <= analysis.stop:
            diag.error(
                "display_time", f"{name}: at {raw.at} is outside the run", src.at(*base, "at")
            )
            return None
    region: tuple[int, int, int, int] | None = None
    if raw.region is not None:
        spec = part.spec.display
        if not isinstance(spec, Ssd1306DisplaySpec):
            diag.error(
                "display_region",
                f"{name}: region counts pixels; {raw.display} is not a pixel display",
                src.at(*base, "region"),
            )
            return None
        x0, y0, x1, y1 = raw.region
        if not (0 <= x0 <= x1 < spec.width and 0 <= y0 <= y1 < spec.height):
            diag.error(
                "display_region",
                f"{name}: region {raw.region} must satisfy 0 <= x0 <= x1 < {spec.width} and "
                f"0 <= y0 <= y1 < {spec.height}",
                src.at(*base, "region"),
            )
            return None
        if raw.min is None and raw.max is None:
            diag.error("missing_bound", f"{name}: region: give min, max, or both", at)
            return None
        region = (x0, y0, x1, y1)
    if not isinstance(part.spec.display, SegmentDisplaySpec) and not runs_firmware:
        diag.error(
            "display_needs_firmware",
            f"{name}: {raw.display} shows what the firmware writes to it; "
            "read it in a test with firmware: true",
            at,
        )
        return None
    bounds = _match_bounds(raw, base, src, diag)
    if bounds is None:
        return None
    return DisplayExpectation(
        index=index,
        ref=raw.display,
        pattern=raw.match,
        region=region,
        at=when,
        min=bounds[0],
        max=bounds[1],
        location=at,
    )


def _has_ac_source(circuit: ResolvedCircuit) -> bool:
    return any(part.values.get("ac", 0.0) != 0.0 for part in circuit.parts.values())


def _compile_analysis(
    name: str,
    raw: OpAnalysis | TranAnalysis | AcAnalysis | DcAnalysis,
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    diag: Diagnostics,
) -> Analysis | None:
    if isinstance(raw, OpAnalysis):
        return Op()
    if isinstance(raw, DcAnalysis):
        return _compile_dc(name, raw, src, circuit, diag)
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
        points = math.log10(stop / start) * raw.points_per_decade
        if points > MAX_AC_POINTS:
            diag.error(
                "too_many_points",
                f"{name}: ac would compute {points:.3g} frequencies (limit {MAX_AC_POINTS}); "
                "narrow start..stop or lower points_per_decade",
                _at(src, name),
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
    points = (stop - (start or 0.0)) / step
    if points > MAX_TRAN_POINTS:
        diag.error(
            "too_many_points",
            f"{name}: tran would record {points:.3g} time points (limit {MAX_TRAN_POINTS:.0e}); "
            "raise step or shorten stop",
            _at(src, name, "step"),
        )
        return None
    return Tran(step=step, stop=stop, start=start)


def _compile_dc(
    name: str,
    raw: DcAnalysis,
    src: SourceMap,
    circuit: ResolvedCircuit | None,
    diag: Diagnostics,
) -> Dc | None:
    """`{ type: dc, sweep: PART.param, start, stop, step }`: the values the parameter takes."""
    ref, dot, param_name = raw.sweep.partition(".")
    if not dot or not ref or not param_name:
        diag.error(
            "invalid_analysis",
            f"{name}: sweep is PART.param, e.g. V1.voltage, got {raw.sweep!r}",
            _at(src, name, "sweep"),
        )
        return None
    if circuit is None:
        return None
    part = circuit.parts.get(ref)
    if part is None:
        diag.error("unknown_part", f"{name}: no part named {ref!r}", _at(src, name, "sweep"))
        return None
    param = part.spec.params.get(param_name)
    if param is None or param.unit is None:
        quantities = [p for p, spec in part.spec.params.items() if spec.unit is not None]
        diag.error(
            "unknown_param",
            f"{name}: {ref} ({part.spec.id}) has no quantity {param_name!r} to sweep; "
            f"quantities: {', '.join(quantities) or 'none'}",
            _at(src, name, "sweep"),
        )
        return None
    unit = param.unit
    bounds: dict[str, float] = {}
    for field in ("start", "stop"):
        parsed = _param_value(
            ref, param_name, param, getattr(raw, field), _at(src, name, field), diag
        )
        if parsed is None:
            return None
        bounds[field] = float(parsed[0])
    try:
        step = parse_quantity(raw.step, unit)
    except QuantityError as exc:
        diag.error("invalid_value", f"{name}.analysis.step: {exc}", _at(src, name, "step"))
        return None
    start, stop = bounds["start"], bounds["stop"]
    if not start < stop or step <= 0:
        diag.error(
            "invalid_analysis", f"{name}: dc needs start < stop and step > 0", _at(src, name)
        )
        return None
    count = math.floor((stop - start) / step * (1 + 1e-9)) + 1
    if count > MAX_DC_POINTS:
        diag.error(
            "too_many_points",
            f"{name}: dc would compute {count:.3g} operating points (limit {MAX_DC_POINTS}); "
            "raise step or narrow start..stop",
            _at(src, name, "step"),
        )
        return None
    values = [float(f"{start + i * step:.12g}") for i in range(count)]
    if stop - values[-1] > step * 1e-6:
        values.append(stop)  # always end on `stop`
    try:
        for value in (start, stop):  # one of them differs from the part's own value
            alter_commands(part, replace(part, values={**part.values, param_name: value}), circuit)
    except ValueError as exc:
        diag.error(
            "sweep_unsupported", f"{name}: cannot sweep {raw.sweep}: {exc}", _at(src, name, "sweep")
        )
        return None
    return Dc(ref=ref, param=param_name, unit=unit, values=tuple(values))


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
    assert raw.measure is not None
    try:
        node = parse_measure(raw.measure)
    except MeasureSyntaxError as exc:
        diag.error("invalid_measure", str(exc), at_measure)
        return None

    ok = True
    measure: Measure | None = None
    if analysis is not None:
        try:
            measure = check(node, domain_of(analysis), x_unit_of(analysis))
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
    window: dict[str, float | None] = {"start": None, "end": None}
    for key, label in (("start", "from"), ("end", "to")):
        text = getattr(raw, key)
        if text is None:
            continue
        if analysis is not None and not isinstance(analysis, Tran):
            diag.error("window_needs_tran", f"{label}: only tran measures take a time range", at)
            ok = False
            continue
        try:
            window[key] = parse_quantity(text, "s")
        except QuantityError as exc:
            diag.error("invalid_value", f"{label}: {exc}", src.at(*base, label))
            ok = False
    start, end = window["start"], window["end"]
    if start is not None and end is not None and start >= end:
        diag.error("invalid_window", f"{raw.measure}: `from` must be before `to`", at)
        ok = False
    if not ok:
        return None
    return CompiledExpectation(
        index=index, measure=measure, min=low, max=high, location=at, start=start, end=end
    )


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
