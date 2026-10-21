"""Display expectations, pictures, and frame files for one simulated test."""

import json
import re
from pathlib import Path

from pydantic import JsonValue

from eda.adapters import rasterize
from eda.display import Screen, thin
from eda.display.segments import SegmentScreen
from eda.model import CompiledTest, Design, DisplayExpectation, Tran
from eda.result import Diagnostics
from eda.schema.part import SegmentDisplaySpec


def segment_screens(
    design: Design, test: CompiledTest, x: list[float] | None, vectors: dict[str, list[float]]
) -> dict[str, Screen]:
    """The segment displays of an op or tran test, decoded from the simulated currents."""
    stop = test.analysis.stop if isinstance(test.analysis, Tran) else None
    return {
        ref: SegmentScreen(part, x, vectors, stop)
        for ref, part in test.circuit(design.circuit).parts.items()
        if isinstance(part.spec.display, SegmentDisplaySpec)
    }


def report(
    design: Design,
    test: CompiledTest,
    screens: dict[str, Screen],
    diag: Diagnostics,
) -> tuple[dict[str, JsonValue], list[JsonValue], bool]:
    """(`displays` for the test's JSON, judged expectations, all passed).

    Writes `out/display/<test>.<ref>.svg/.png` (the last frame) and `.json` (every frame)."""
    out = design.paths.out_dir / "display"
    out.mkdir(parents=True, exist_ok=True)
    shown: dict[str, JsonValue] = {}
    for ref, screen in screens.items():
        shown[ref] = _write(design, test, ref, screen, out)
    checks: list[JsonValue] = []
    passed = True
    for exp in test.displays:
        screen = screens.get(exp.ref)
        check = _judge(exp, screen, test, diag)
        checks.append(check)
        passed = passed and check["passed"] is True
    return shown, checks, passed


def _write(
    design: Design, test: CompiledTest, ref: str, screen: Screen, out: Path
) -> dict[str, JsonValue]:
    last = screen.frame_at(None)
    stem = f"{test.name}.{ref}"
    svg = out / f"{stem}.svg"
    svg.write_text(screen.svg(last), encoding="utf-8")
    png = out / f"{stem}.png"
    try:
        rasterize.svg_to_png(svg, png, design.paths.out_dir / ".chrome-profile")
    except RuntimeError:
        png.unlink(missing_ok=True)
    frames = thin(screen.timeline())
    timeline: list[JsonValue] = [{"time": f.time, "text": f.text, "state": f.state} for f in frames]
    frames_path = out / f"{stem}.json"
    frames_path.write_text(
        json.dumps({"ref": ref, "kind": screen.kind, "meta": screen.meta, "frames": timeline}),
        encoding="utf-8",
    )
    changes: list[JsonValue] = []
    for frame in frames:
        if not changes or _last_text(changes) != frame.text:
            changes.append({"time": frame.time, "text": frame.text})
    return {
        "kind": screen.kind,
        "text": last.text,
        "changes": changes[-50:],
        "image": design.paths.rel(png) if png.is_file() else design.paths.rel(svg),
        "frames": design.paths.rel(frames_path),
    }


def _last_text(changes: list[JsonValue]) -> JsonValue:
    last = changes[-1]
    assert isinstance(last, dict)
    return last["text"]


def _judge(
    exp: DisplayExpectation, screen: Screen | None, test: CompiledTest, diag: Diagnostics
) -> dict[str, JsonValue]:
    text = screen.frame_at(exp.at).text if screen is not None else ""
    pattern = re.compile(exp.pattern, re.MULTILINE)
    matches = list(pattern.finditer(text))
    value: float | None
    if exp.min is None and exp.max is None:
        value = 1.0 if matches else 0.0
        passed = bool(matches)
        display = repr(matches[0].group(0)) if matches else "no match"
    else:
        if pattern.groups:
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
        why = "the display shows nothing" if screen is None else f"the screen shows {text!r}"
        diag.error(
            "expect",
            f"{test.name}: {exp.text} = {display}, expected {exp.range_text} ({why})",
            exp.location,
            test=test.name,
            measure=exp.text,
            value=value,
            min=exp.min,
            max=exp.max,
            screen=text,
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
        "screen": text,
    }
