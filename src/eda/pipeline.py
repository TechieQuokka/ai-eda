"""The `check`, `sim`, `render`, and `run` commands as functions returning a `Result`."""

from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from eda import firmware, simulate
from eda import pcb as board
from eda.adapters import netlistsvg, picosdk, rasterize
from eda.design import load_design
from eda.model import CompiledTest, Design, Op
from eda.render import schematic, waveform
from eda.result import Diagnostics, Result
from eda.summary import summarize
from eda.verify.measure import db_values, phase_values, probes


def check(root: Path) -> tuple[Result, Design | None]:
    diag = Diagnostics()
    design = load_design(root, diag)
    data: dict[str, JsonValue] = {}
    if design is not None:
        data = _summary(design)
    return _finish(root, "check", diag, data), design


def sim(root: Path, test: str | None = None) -> Result:
    diag = Diagnostics()
    design = load_design(root, diag)
    data: dict[str, JsonValue] = {}
    if design is not None:
        data = simulate.run_tests(design, diag, test)
    return _finish(root, "sim", diag, data)


def render(root: Path) -> Result:
    diag = Diagnostics()
    design = load_design(root, diag)
    data: dict[str, JsonValue] = {}
    if design is not None:
        data = _render(design, diag)
    return _finish(root, "render", diag, data)


def fw(root: Path, build: bool = False) -> Result:
    diag = Diagnostics()
    design = load_design(root, diag)
    data: dict[str, JsonValue] = {}
    if design is not None:
        data = firmware.generate(design, diag)
        if build and diag.ok:
            data["build"] = firmware.build(design, diag)
    return _finish(root, "fw", diag, data)


def pcb(root: Path) -> Result:
    diag = Diagnostics()
    design = load_design(root, diag)
    data: dict[str, JsonValue] = {}
    if design is not None:
        data = board.build(design, diag)
    return _finish(root, "pcb", diag, data)


def run(root: Path) -> Result:
    """check (→ fw + build) → sim → render (→ pcb) in one call; fw and pcb run when
    design/firmware.yaml and design/board.yaml exist.

    Stops after check if the design has errors."""
    diag = Diagnostics()
    design = load_design(root, diag)
    data: dict[str, JsonValue] = {}
    if design is not None:
        data["check"] = _summary(design)
        _write(root, "check", diag.to_result("check", root.name, data["check"]))
        image: Path | None = None
        if design.firmware is not None:
            fw_diag = Diagnostics()
            fw_data = firmware.generate(design, fw_diag)
            if fw_diag.ok:
                build = _build_if_possible(design, fw_diag)
                fw_data["build"] = build
                uf2 = build.get("uf2")
                image = root / uf2 if isinstance(uf2, str) else None
            data["fw"] = fw_data
            _write(root, "fw", fw_diag.to_result("fw", root.name, fw_data))
            diag.extend(fw_diag)
        sim_diag = Diagnostics()
        data["sim"] = simulate.run_tests(design, sim_diag, image=image, build_firmware=False)
        _write(root, "sim", sim_diag.to_result("sim", root.name, data["sim"]))
        render_diag = Diagnostics()
        data["render"] = _render(design, render_diag)
        _write(root, "render", render_diag.to_result("render", root.name, data["render"]))
        diag.extend(sim_diag)
        diag.extend(render_diag)
        if design.board is not None:
            pcb_diag = Diagnostics()
            data["pcb"] = board.build(design, pcb_diag)
            _write(root, "pcb", pcb_diag.to_result("pcb", root.name, data["pcb"]))
            diag.extend(pcb_diag)
    else:
        _write(root, "check", diag.to_result("check", root.name, {}))
    return _finish(root, "run", diag, data)


def _build_if_possible(design: Design, diag: Diagnostics) -> dict[str, JsonValue]:
    """`run` builds the firmware when the pico-sdk is installed and skips it otherwise."""
    try:
        picosdk.sdk_path()
    except picosdk.PicoSdkError as exc:
        diag.warning("fw_build_skipped", f"firmware not compiled: {exc}")
        return {}
    return firmware.build(design, diag)


def _finish(root: Path, command: str, diag: Diagnostics, data: dict[str, JsonValue]) -> Result:
    result = diag.to_result(command, root.name, data)
    _write(root, command, result)
    return result


def _write(root: Path, command: str, result: Result) -> None:
    out = root / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{command}.json").write_text(result.model_dump_json(indent=1), encoding="utf-8")


def _summary(design: Design) -> dict[str, JsonValue]:
    return summarize(design).model_dump(mode="json")


def _plot_series(
    test: CompiledTest, data: simulate.WaveformData
) -> tuple[list[waveform.Trace], list[waveform.Bound]]:
    """Traces for every probe the test measures, plus its bounds as reference lines."""
    signals = data.as_signals()
    frequency = data.axis == "frequency"
    x_unit = "Hz" if frequency else "s"
    traces: list[waveform.Trace] = []
    bounds: list[waveform.Bound] = []

    def add(trace: waveform.Trace) -> None:
        if all(t.name != trace.name for t in traces):
            traces.append(trace)

    for exp in test.expectations:
        for probe in probes(exp.measure.node):
            values = simulate.probe_values(probe, signals, len(data.x))
            if values is None:
                continue
            if frequency:
                add(waveform.Trace(f"db({probe.text})", "dB", db_values(values)))
                add(waveform.Trace(f"phase({probe.text})", "deg", phase_values(values)))
            else:
                real = [v.real if isinstance(v, complex) else v for v in values]
                add(waveform.Trace(probe.text, probe.unit, real))
        if exp.measure.reduction == "period":
            continue  # a duration, not a position on either axis
        unit = exp.measure.unit
        axis: Literal["x", "y"] = "x" if unit == x_unit else "y"
        label = f"{exp.measure.text}: {exp.range_text}"
        # One label per expectation, on its upper bound (or its only bound).
        if exp.min is not None:
            bounds.append(waveform.Bound(label if exp.max is None else "", unit, exp.min, axis))
        if exp.max is not None:
            bounds.append(waveform.Bound(label, unit, exp.max, axis))
    return traces, bounds


def _render(design: Design, diag: Diagnostics) -> dict[str, JsonValue]:
    out = design.paths.out_dir
    out.mkdir(parents=True, exist_ok=True)
    rel = design.paths.rel
    images: dict[str, JsonValue] = {}

    svg_path = out / "schematic.svg"
    try:
        netlistsvg.render(
            schematic.to_netlistsvg(design.project.name, design.circuit),
            out / "schematic.netlistsvg.json",
            svg_path,
            schematic.SKIN,
        )
        drawn = svg_path.read_text(encoding="utf-8")
        drawn = schematic.pad_svg(schematic.label_nets(drawn, design.circuit))
        svg_path.write_text(drawn, encoding="utf-8")
        images["schematic"] = rel(svg_path)
    except (netlistsvg.NetlistsvgUnavailable, RuntimeError, OSError) as exc:
        diag.fail_tool("render_schematic", str(exc))

    waves: dict[str, JsonValue] = {}
    waves_dir = out / "waves"
    for test in design.tests.values():
        if isinstance(test.analysis, Op):
            continue
        source = out / "sim" / f"{test.name}.json"
        if not source.is_file():
            diag.warning(
                "no_sim_data",
                f"{test.name}: no simulation data yet; run `eda sim` first",
                test.location,
            )
            continue
        try:
            data = simulate.WaveformData.model_validate_json(source.read_text(encoding="utf-8"))
        except ValueError as exc:
            diag.warning("bad_sim_data", f"{rel(source)}: {exc}; run `eda sim` again")
            continue
        traces, bounds = _plot_series(test, data)
        if not traces:
            continue
        waves_dir.mkdir(exist_ok=True)
        wave_svg = waves_dir / f"{test.name}.svg"
        frequency = data.axis == "frequency"
        wave_svg.write_text(
            waveform.plot(
                f"{design.project.name} / {test.name}",
                data.x,
                traces,
                bounds,
                x_unit="Hz" if frequency else "s",
                log_x=frequency,
            ),
            encoding="utf-8",
        )
        waves[test.name] = rel(wave_svg)
    images["waveforms"] = waves

    pngs: dict[str, JsonValue] = {}
    svgs = [svg_path, *(waves_dir / f"{name}.svg" for name in waves)]
    if rasterize.available() is None:
        diag.warning("no_rasterizer", "PNG copies skipped: no rsvg-convert or Chrome found")
    else:
        for svg in svgs:
            if not svg.is_file():
                continue
            try:
                rasterize.svg_to_png(svg, svg.with_suffix(".png"), out / ".chrome-profile")
                pngs[rel(svg)] = rel(svg.with_suffix(".png"))
            except (RuntimeError, OSError) as exc:
                diag.warning("rasterize_failed", f"{rel(svg)}: {exc}")
    images["png"] = pngs
    images["rasterizer"] = rasterize.available()
    return images
