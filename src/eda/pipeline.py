"""The `check`, `sim`, `render`, and `run` commands as functions returning a `Result`."""

from pathlib import Path

from pydantic import JsonValue

from eda import simulate
from eda.adapters import netlistsvg, rasterize
from eda.design import load_design
from eda.model import Design, Op
from eda.render import schematic, waveform
from eda.result import Diagnostics, Result
from eda.summary import summarize


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


def run(root: Path) -> Result:
    """check → sim → render in one call. Stops after check if the design has errors."""
    diag = Diagnostics()
    design = load_design(root, diag)
    data: dict[str, JsonValue] = {}
    if design is not None:
        data["check"] = _summary(design)
        _write(root, "check", diag.to_result("check", root.name, data["check"]))
        sim_diag = Diagnostics()
        data["sim"] = simulate.run_tests(design, sim_diag)
        _write(root, "sim", sim_diag.to_result("sim", root.name, data["sim"]))
        render_diag = Diagnostics()
        data["render"] = _render(design, render_diag)
        _write(root, "render", render_diag.to_result("render", root.name, data["render"]))
        diag.extend(sim_diag)
        diag.extend(render_diag)
    else:
        _write(root, "check", diag.to_result("check", root.name, {}))
    return _finish(root, "run", diag, data)


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
        signals = data.as_signals()
        traces: list[waveform.Trace] = []
        bounds: list[waveform.Bound] = []
        for exp in test.expectations:
            probe = exp.measure.probe
            values = simulate.probe_values(probe, signals, len(data.time))
            if values is not None and all(t.name != probe.text for t in traces):
                traces.append(waveform.Trace(probe.text, probe.unit, values))
            for key, bound in (("min", exp.min), ("max", exp.max)):
                if bound is not None:
                    bounds.append(waveform.Bound(f"{exp.measure.text} {key}", probe.unit, bound))
        if not traces:
            continue
        waves_dir.mkdir(exist_ok=True)
        wave_svg = waves_dir / f"{test.name}.svg"
        wave_svg.write_text(
            waveform.plot(f"{design.project.name} / {test.name}", data.time, traces, bounds),
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
