"""Board generation: design → KiCad board → autorouting → DRC → fabrication files.

Our YAML is the source of truth (DESIGN_DECISIONS.md §13); everything under `out/pcb/`
is regenerated on every run.
"""

import csv
import hashlib
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from eda import fab as fabs
from eda.adapters import freerouting, kicad, rasterize
from eda.fab import Fab
from eda.model import Design, ResolvedBoard
from eda.result import Diagnostics, Location

# DRC checks that only concern KiCad's library bookkeeping, not the board itself.
_IGNORED_DRC = frozenset({"lib_footprint_issues", "lib_footprint_mismatch"})
# Reported as warnings whatever KiCad says: a thermal relief that reaches only a fill island
# is a soldering/quality concern; missing connections are caught as `unconnected` items.
_WARNING_DRC = frozenset({"starved_thermal"})
_REF_IN_TEXT = re.compile(r"\bof ([A-Za-z_][A-Za-z0-9_]*)\b")


def build(design: Design, diag: Diagnostics) -> dict[str, JsonValue]:
    board = design.board
    if board is None:
        diag.error(
            "no_board",
            "no design/board.yaml; create one (it may be just `board: {}`) to generate a PCB",
        )
        return {}
    paths = design.paths
    out = paths.out_dir / "pcb"
    out.mkdir(parents=True, exist_ok=True)
    name = design.project.name
    pcb = out / f"{name}.kicad_pcb"
    dsn, ses = out / f"{name}.dsn", out / f"{name}.ses"
    rel = paths.rel

    try:
        layout = kicad.pcbnew(_build_request(board, pcb, dsn))
    except kicad.KicadDesignError as exc:
        if "overlaps" in str(exc):
            diag.error(
                "placement_overlap",
                f"{exc}: move one of them in design/board.yaml (placement x/y is the "
                "footprint's origin, usually pin 1)",
                board.source.at("placement", exc.ref),
                part=exc.ref,
            )
            return {}
        diag.error(
            "no_room",
            f"{exc}: give board.width / board.height more room, move hand-placed parts, "
            "or leave the size out so the board fits the parts",
            board.source.at("board"),
            part=exc.ref,
        )
        return {}
    except (kicad.KicadError, OSError, subprocess.TimeoutExpired) as exc:
        diag.fail_tool("pcb_build", f"could not create the KiCad board: {exc}")
        return {}

    try:
        cached = _route_cached(dsn, ses, board.passes, out)
        kicad.pcbnew({"command": "import_ses", "pcb": str(pcb), "ses": str(ses)})
        removed = 0
        for _ in range(20):
            step = int(kicad.pcbnew({"command": "remove_stubs", "pcb": str(pcb)})["removed"])
            removed += step
            if step == 0:
                break
        poured: JsonValue = []
        if board.pours:
            request = {"command": "pour", "pcb": str(pcb), "pours": _pour_request(board)}
            poured = _json(kicad.pcbnew(request)["pours"])
        stats = kicad.pcbnew({"command": "inspect", "pcb": str(pcb)})
        stats["removed_stubs"] = removed
    except (freerouting.FreeroutingUnavailable, kicad.KicadError, RuntimeError) as exc:
        diag.fail_tool("autoroute", f"autorouting failed: {exc}")
        return {"pcb": rel(pcb), "layout": _json(layout)}
    except (OSError, subprocess.TimeoutExpired) as exc:
        diag.fail_tool("autoroute", f"autorouting failed: {exc}")
        return {"pcb": rel(pcb), "layout": _json(layout)}

    if board.fab is not None:
        _check_fab(design, board.fab, layout, stats, diag)

    unrouted = int(stats.get("unrouted", 0))
    if unrouted:
        diag.error(
            "unrouted",
            f"{unrouted} connection(s) could not be routed; move parts apart, enlarge the "
            "board, or raise routing.passes in design/board.yaml",
            board.source.at("board"),
            unrouted=unrouted,
        )

    try:
        report = kicad.drc(pcb, out / "drc.json")
    except (kicad.KicadError, OSError, subprocess.TimeoutExpired) as exc:
        diag.fail_tool("drc", str(exc))
        report = {}
    drc_summary = _drc_issues(report, design, diag)

    outputs: dict[str, JsonValue] = {}
    try:
        fab = kicad.export_fabrication(pcb, out / "fab")
        bom = _write_bom(design, board, out / "fab" / f"{name}-bom.csv", diag)
        archive = out / f"{name}-fab.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in [*fab, bom]:
                zf.write(file, file.name)
        outputs["fabrication"] = rel(archive)
        outputs["files"] = [rel(f) for f in [*fab, bom]]
        svg = paths.out_dir / "board.svg"
        kicad.export_svg(pcb, svg)
        outputs["svg"] = rel(svg)
        assembly = paths.out_dir / "assembly.svg"
        kicad.export_assembly_svg(pcb, assembly)
        outputs["assembly_svg"] = rel(assembly)
        if rasterize.available() is not None:
            for image, key in ((svg, "png"), (assembly, "assembly_png")):
                png = image.with_suffix(".png")
                rasterize.svg_to_png(image, png, paths.out_dir / ".chrome-profile", width=1200)
                outputs[key] = rel(png)
        render = paths.out_dir / "board-3d.png"
        kicad.render_3d(pcb, render)
        outputs["render_3d"] = rel(render)
    except (kicad.KicadError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        diag.fail_tool("pcb_export", str(exc))

    x0, y0, w, h = (float(v) for v in layout.get("outline", [0, 0, 0, 0]))
    return {
        "pcb": rel(pcb),
        "size_mm": [round(w, 2), round(h, 2)],
        "origin_mm": [round(x0, 2), round(y0, 2)],
        "footprints": _json(stats.get("footprints", {})),
        "routing": {
            "cached": cached,
            "tracks": int(stats.get("tracks", 0)),
            "vias": int(stats.get("vias", 0)),
            "track_length_mm": float(stats.get("track_length_mm", 0.0)),
            "hpwl_mm": float(stats.get("hpwl_mm", 0.0)),
            "unrouted": unrouted,
        },
        "pours": poured,
        "drc": drc_summary,
        "fab": board.fab.name if board.fab is not None else None,
        "outputs": outputs,
    }


def _route_cached(dsn: Path, ses: Path, passes: int, out: Path) -> bool:
    """Route with Freerouting, reusing the session for an identical board.

    Freerouting is not deterministic (two runs on the same .dsn give different tracks), so
    the session is cached by the hash of its input; the same design then always yields the
    same board. Returns True when the cache was used.
    """
    key = hashlib.sha256(dsn.read_bytes() + f"|{passes}|{freerouting.JAR.name}".encode())
    cache = out / "route-cache" / f"{key.hexdigest()[:32]}.ses"
    if cache.is_file():
        shutil.copyfile(cache, ses)
        return True
    freerouting.route(dsn, ses, passes, out / "freerouting.log")
    cache.parent.mkdir(exist_ok=True)
    shutil.copyfile(ses, cache)
    return False


def _pour_request(board: ResolvedBoard) -> list[dict[str, Any]]:
    return [
        {
            "net": pour.net,
            "layers": list(pour.layers),
            "clearance": pour.clearance,
            "min_width": pour.min_width,
        }
        for pour in board.pours
    ]


def _build_request(board: ResolvedBoard, pcb: Path, dsn: Path) -> dict[str, Any]:
    nets = sorted({net for part in board.parts for net in part.pads.values()})
    footprints: list[dict[str, Any]] = []
    for part in board.parts:
        item: dict[str, Any] = {
            "ref": part.ref,
            "value": part.value,
            "footprint": part.footprint,
            "pads": part.pads,
            "joined_pads": part.joined_pads,
        }
        if part.placement is not None:
            item |= {
                "x": part.placement.x,
                "y": part.placement.y,
                "rotation": part.placement.rotation,
                "side": part.placement.side,
            }
        footprints.append(item)
    return {
        "command": "build",
        "pcb": str(pcb),
        "dsn": str(dsn),
        "rules": {
            "clearance": board.rules.clearance,
            "track_width": board.rules.track_width,
            "via_diameter": board.rules.via_diameter,
            "via_drill": board.rules.via_drill,
            "track_widths": dict(board.rules.track_widths),
            "layers": board.layers,
        },
        "board": {
            "width": board.width,
            "height": board.height,
            "margin": board.margin,
            "spacing": board.spacing,
            "placer": board.placer,
        },
        "nets": nets,
        "footprints": footprints,
    }


def _drc_issues(report: dict[str, Any], design: Design, diag: Diagnostics) -> dict[str, JsonValue]:
    counts: dict[str, int] = {"errors": 0, "warnings": 0, "unconnected": 0}
    groups: list[tuple[str, list[dict[str, Any]]]] = [
        ("violation", list(report.get("violations", []))),
        ("unconnected", list(report.get("unconnected_items", []))),
    ]
    for kind, items in groups:
        for item in items:
            rule = str(item.get("type", "unknown"))
            if rule in _IGNORED_DRC:
                continue
            parts = list(item.get("items", []))
            texts = [str(p.get("description", "")) for p in parts]
            positions: list[JsonValue] = [
                [round(float(p["pos"]["x"]), 3), round(float(p["pos"]["y"]), 3)]
                for p in parts
                if "pos" in p
            ]
            message = f"DRC {rule}: {item.get('description', '')}"
            if texts:
                message += " — " + "; ".join(texts)
            at = _locate(texts, design)
            severity = "warning" if rule in _WARNING_DRC else str(item.get("severity", "error"))
            if kind == "unconnected":
                counts["unconnected"] += 1
            if severity == "error" or kind == "unconnected":
                counts["errors"] += 1
                diag.error(f"drc_{rule}", message, at, at_mm=positions)
            else:
                counts["warnings"] += 1
                diag.warning(f"drc_{rule}", message, at, at_mm=positions)
    return {"report": "out/pcb/drc.json", **counts}


def _locate(texts: list[str], design: Design) -> Location | None:
    """Point a DRC issue at the first part it mentions: its placement, else its definition."""
    for text in texts:
        for ref in _REF_IN_TEXT.findall(text):
            board = design.board
            if board is not None and any(
                p.ref == ref and p.placement is not None for p in board.parts
            ):
                return board.source.at("placement", ref)
            if ref in design.circuit.parts:
                return design.circuit.source.at("parts", ref)
    return None


def _check_fab(
    design: Design, fab: Fab, layout: dict[str, Any], stats: dict[str, Any], diag: Diagnostics
) -> None:
    """What only the built board shows: its size and the holes of its footprints."""
    board = design.board
    assert board is not None
    _, _, width, height = (float(v) for v in layout.get("outline", [0, 0, 0, 0]))
    fabs.check_size(fab, width, height, diag, board.source.at("board"))
    footprints: dict[str, dict[str, Any]] = stats.get("footprints", {})
    for ref, report in sorted(footprints.items()):
        at = design.circuit.source.at("parts", ref) if ref in design.circuit.parts else None
        for key, limit, what in (
            ("drill_mm", "min_drill", "smallest plated hole"),
            ("ring_mm", "min_pad_ring", "thinnest pad ring"),
            ("npth_mm", "min_npth_drill", "smallest non-plated hole"),
        ):
            if key in report:
                fabs.below(fab, diag, f"{ref}'s {what}", float(report[key]), limit, at, part=ref)


def _write_bom(design: Design, board: ResolvedBoard, path: Path, diag: Diagnostics) -> Path:
    """A shopping list: one row per distinct item to buy, with its quantity and references."""
    groups: dict[tuple[str, str, str, str, str], list[str]] = {}
    for part in board.parts:
        resolved = design.circuit.parts[part.ref]
        spec = resolved.spec
        mpn = resolved.mpn or ""
        if spec.category == "source":
            # On a board a source is the connector where the supply or signal comes in.
            row = ("Connector", "", mpn, part.footprint, "supply / signal input")
        else:
            note = ""
            if spec.generic and not mpn:
                note = "generic model: choose a real part"
                diag.warning(
                    "bom_generic_part",
                    f"{part.ref} is a generic {spec.name} model, not a real part; set `mpn:` "
                    "(the part number to buy) on it in circuit.yaml",
                    design.circuit.source.at("parts", part.ref),
                    part=part.ref,
                )
            row = (spec.name, part.value, mpn, part.footprint, note)
        groups.setdefault(row, []).append(part.ref)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Qty", "References", "Part", "Value", "MPN", "Footprint", "Note"])
        for (name, value, mpn, footprint, note), refs in sorted(groups.items()):
            writer.writerow([len(refs), " ".join(refs), name, value, mpn, footprint, note])
    return path


def _json(value: Any) -> JsonValue:  # noqa: ANN401 - data from KiCad's JSON reply
    result: JsonValue = value
    return result
