"""Runs under KiCad's own Python (system python3 with the `pcbnew` module), not under eda.

eda's `adapters/kicad.py` calls this file as a subprocess with a JSON request on stdin and
reads one JSON object from stdout. Commands:

  build       create a .kicad_pcb from footprints, nets, placement, outline, and rules,
              then export a Specctra .dsn for the autorouter
  import_ses  load the board, import the autorouter's .ses session, save
  remove_stubs  delete dangling track stubs once (repeat until it removes nothing)
  pour        add copper fills (zones) over the board for given nets and fill them
  inspect     report footprint positions, sizes, and routed/unrouted connection counts

Keep this file free of eda imports and third-party packages.
"""

import importlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

# KiCad's module has no type information; everything from it is Any.
pcbnew: Any = importlib.import_module("pcbnew")

type Request = dict[str, Any]
type Box = tuple[float, float, float, float]
type Shape = tuple[float, Box, list[tuple[str, float, float]]]  # rotation, courtyard, pads

ROUTER_MARGIN_UM = 10.0  # extra clearance the router keeps beyond the DRC rule
FOOTPRINT_DIR = Path(os.environ.get("KICAD10_FOOTPRINT_DIR", "/usr/share/kicad/footprints"))


def MM(value: float) -> int:
    return int(pcbnew.FromMM(value))


def _point(x: float, y: float) -> Any:
    return pcbnew.VECTOR2I(MM(x), MM(y))


def _load_footprint(lib_id: str) -> Any:
    lib, _, name = lib_id.partition(":")
    path = FOOTPRINT_DIR / (lib + ".pretty")
    footprint = pcbnew.FootprintLoad(str(path), name)
    if footprint is None:
        raise ValueError("footprint not found: " + lib_id)
    return footprint


def _apply_rules(board: Any, rules: Request) -> None:
    settings = board.GetDesignSettings()
    netclass = settings.m_NetSettings.GetDefaultNetclass()
    netclass.SetClearance(MM(rules["clearance"]))
    netclass.SetTrackWidth(MM(rules["track_width"]))
    netclass.SetViaDiameter(MM(rules["via_diameter"]))
    netclass.SetViaDrill(MM(rules["via_drill"]))
    settings.SetCopperLayerCount(rules.get("layers", 2))
    settings.m_MinClearance = MM(rules["clearance"])
    settings.m_TrackMinWidth = MM(
        min([rules["track_width"], *rules.get("track_widths", {}).values()])
    )


def _assign_track_widths(board: Any, widths: dict[str, float]) -> None:
    """One net class per extra track width (the router reads the classes from the .dsn);
    each listed net is assigned to its class by name. Clearance and vias stay the default
    class's."""
    net_settings = board.GetDesignSettings().m_NetSettings
    default = net_settings.GetDefaultNetclass()
    for net, width in widths.items():
        name = f"track_{width:g}mm"
        if not net_settings.HasNetclass(name):
            netclass = pcbnew.NETCLASS(name)
            netclass.SetTrackWidth(MM(width))
            netclass.SetClearance(default.GetClearance())
            netclass.SetViaDiameter(default.GetViaDiameter())
            netclass.SetViaDrill(default.GetViaDrill())
            net_settings.SetNetclass(name, netclass)
        net_settings.SetNetclassPatternAssignment(net, name)
    net_settings.RecomputeEffectiveNetclasses()
    board.SynchronizeNetsAndNetClasses(True)


def _courtyard(footprint: Any) -> Box:
    """Bounding box (mm) of the footprint's courtyard and pads, at its position.

    Footprints without a courtyard fall back to their full bounding box (which can include
    fabrication drawings far beyond the part, e.g. the Pico's debug-header outline)."""
    box = None
    for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
        outline = footprint.GetCourtyard(layer)
        if outline.OutlineCount():
            part = outline.BBox()
            box = part if box is None else box.Merge(part) or box
    if box is None:
        box = footprint.GetBoundingBox(False)
    else:
        for pad in footprint.Pads():
            box.Merge(pad.GetBoundingBox())
    return (
        pcbnew.ToMM(box.GetX()),
        pcbnew.ToMM(box.GetY()),
        pcbnew.ToMM(box.GetWidth()),
        pcbnew.ToMM(box.GetHeight()),
    )


def _auto_place(
    placed: list[tuple[Any, Box]], unplaced: list[Any], width: float, gap: float
) -> None:
    """Shelf packing, largest parts first (so small parts fill the shelf beside them),
    left to right, top to bottom, starting below any hand-placed parts."""
    x, y, shelf = gap, gap, 0.0
    if placed:
        y = max(box[1] + box[3] for _, box in placed) + gap

    def area(footprint: Any) -> float:
        footprint.SetPosition(_point(0, 0))
        _, _, w, h = _courtyard(footprint)
        return w * h

    for footprint in sorted(unplaced, key=area, reverse=True):
        footprint.SetPosition(_point(0, 0))
        bx, by, bw, bh = _courtyard(footprint)
        if x + bw > width - gap and x > gap:
            x, y, shelf = gap, y + shelf + gap, 0.0
        # Move so the bounding box's top-left corner lands at (x, y).
        footprint.SetPosition(_point(x - bx, y - by))
        x += bw + gap
        shelf = max(shelf, bh)


class DesignProblem(Exception):
    """The board as specified cannot be built (a fault of the design, not of the tool)."""

    def __init__(self, message: str, ref: str) -> None:
        super().__init__(message)
        self.ref = ref


ROTATIONS = (0.0, 90.0, 180.0, 270.0)
GRID_MM = 0.5  # candidate positions are on this grid
# A large area coarsens the grid (in GRID_MM multiples) so one part never tries more than
# this many positions; boards up to about 100 mm square keep the full GRID_MM grid.
MAX_CANDIDATES = 40_000
AREA_WEIGHT = 0.5  # cost of growing the layout's half-perimeter, relative to wire length


def _shape(footprint: Any, rotation: float) -> tuple[Box, list[tuple[str, float, float]]]:
    """Courtyard box and (net, x, y) of each connected pad, relative to the footprint origin."""
    footprint.SetOrientationDegrees(rotation)
    footprint.SetPosition(_point(0, 0))
    box = _courtyard(footprint)
    pads: list[tuple[str, float, float]] = [
        (
            str(pad.GetNetname()),
            float(pcbnew.ToMM(pad.GetPosition().x)),
            float(pcbnew.ToMM(pad.GetPosition().y)),
        )
        for pad in footprint.Pads()
        if pad.GetNetname()
    ]
    return box, pads


def _overlaps(box: Box, others: list[Box], gap: float) -> bool:
    x, y, w, h = box
    for ox, oy, ow, oh in others:
        if x < ox + ow + gap and ox < x + w + gap and y < oy + oh + gap and oy < y + h + gap:
            return True
    return False


def _connected_place(
    placed: list[tuple[Any, Box]],
    unplaced: list[Any],
    gap: float,
    bounds: Box | None,
) -> None:
    """Greedy placement that keeps connected parts together.

    Parts go one at a time, the one with the most connections to already placed parts
    first (ties: larger first, then by reference). Each takes the grid position and
    rotation with the lowest cost: the growth of every net's bounding box (half-perimeter
    wire length) plus AREA_WEIGHT times the growth of the layout's own half-perimeter.
    `bounds` (x, y, w, h) confines parts to a fixed board; otherwise the layout grows.
    """
    boxes = [box for _, box in placed]
    net_boxes: dict[str, list[float]] = {}

    def add_pad(net: str, x: float, y: float) -> None:
        b = net_boxes.get(net)
        if b is None:
            net_boxes[net] = [x, y, x, y]
        else:
            b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)

    for footprint, _ in placed:
        for pad in footprint.Pads():
            if pad.GetNetname():
                pos = pad.GetPosition()
                add_pad(pad.GetNetname(), pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y))

    shapes: dict[int, list[Shape]] = {}
    for fp in unplaced:
        variants: list[Shape] = []
        for rotation in ROTATIONS:
            box, pads = _shape(fp, rotation)
            variants.append((rotation, box, pads))
        shapes[id(fp)] = variants
    remaining = list(unplaced)
    while remaining:

        def priority(fp: Any) -> tuple[int, float, str]:
            _, (_, _, w, h), pads = shapes[id(fp)][0]
            linked = sum(1 for net, _, _ in pads if net in net_boxes)
            return (-linked, -w * h, fp.GetReference())

        remaining.sort(key=priority)
        footprint = remaining.pop(0)
        layout: Box | None = None  # x0, y0, x1, y1 of everything placed so far
        if boxes:
            layout = (
                float(min(b[0] for b in boxes)),
                float(min(b[1] for b in boxes)),
                float(max(b[0] + b[2] for b in boxes)),
                float(max(b[1] + b[3] for b in boxes)),
            )
        best: tuple[float, float, float, float] | None = None  # cost, x, y, rotation
        for rotation, (bx, by, bw, bh), pads in shapes[id(footprint)]:
            # Range of footprint origins whose box stays in the allowed area.
            if bounds is not None:
                area = (
                    bounds[0],
                    bounds[1],
                    bounds[0] + bounds[2] - bw,
                    bounds[1] + bounds[3] - bh,
                )
            elif layout is not None:
                area = (
                    layout[0] - bw - gap,
                    layout[1] - bh - gap,
                    layout[2] + gap,
                    layout[3] + gap,
                )
            else:
                area = (0.0, 0.0, 0.0, 0.0)
            x_lo, y_lo = float(area[0] - bx), float(area[1] - by)
            x_hi, y_hi = float(area[2] - bx), float(area[3] - by)
            span = max(0.0, x_hi - x_lo) * max(0.0, y_hi - y_lo)
            grid = GRID_MM * max(1, math.ceil(math.sqrt(span / MAX_CANDIDATES) / GRID_MM))
            steps_x = max(0, int((x_hi - x_lo) / grid))
            steps_y = max(0, int((y_hi - y_lo) / grid))
            for i in range(steps_x + 1):
                x = x_lo + i * grid
                for j in range(steps_y + 1):
                    y = y_lo + j * grid
                    box = (x + bx, y + by, bw, bh)
                    if _overlaps(box, boxes, gap):
                        continue
                    cost = 0.0
                    for net, px, py in pads:
                        nb = net_boxes.get(net)
                        if nb is None:
                            continue
                        ax, ay = x + px, y + py
                        cost += (max(nb[2], ax) - min(nb[0], ax)) - (nb[2] - nb[0])
                        cost += (max(nb[3], ay) - min(nb[1], ay)) - (nb[3] - nb[1])
                    if layout is not None:
                        grown = (
                            max(layout[2], box[0] + bw)
                            - min(layout[0], box[0])
                            + max(layout[3], box[1] + bh)
                            - min(layout[1], box[1])
                        )
                        cost += AREA_WEIGHT * (
                            grown - (layout[2] - layout[0] + layout[3] - layout[1])
                        )
                    if best is None or cost < best[0] - 1e-9:
                        best = (cost, x, y, rotation)
        if best is None:
            raise DesignProblem(
                "no room for " + footprint.GetReference() + " on the board; enlarge it",
                footprint.GetReference(),
            )
        _, x, y, rotation = best
        footprint.SetOrientationDegrees(rotation)
        footprint.SetPosition(_point(x, y))
        boxes.append(_courtyard(footprint))
        for pad in footprint.Pads():
            if pad.GetNetname():
                pos = pad.GetPosition()
                add_pad(pad.GetNetname(), pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y))


def _target_width(footprints: list[Any], gap: float) -> float:
    """Packing width for boards without a fixed size: aim for a roughly 3:2 board."""
    sizes: list[tuple[float, float]] = []
    for footprint in footprints:
        footprint.SetPosition(_point(0, 0))
        _, _, w, h = _courtyard(footprint)
        sizes.append((w + gap, h + gap))
    if not sizes:
        return 100.0
    area = sum(w * h for w, h in sizes)
    widest = max(w for w, _ in sizes)
    return max(widest + gap, (area * 1.5) ** 0.5 * 1.25)


def _clear_fab_reference(footprint: Any) -> None:
    """Move the assembly-drawing reference (F.Fab) where the silkscreen one sits when it
    would cover pads, as on small connectors; the library places that one clear of them."""
    pads = [pad.GetBoundingBox() for pad in footprint.Pads()]
    silk = footprint.Reference()
    for item in footprint.GraphicalItems():
        if item.GetClass() != "PCB_TEXT" or item.GetLayer() != pcbnew.F_Fab:
            continue
        if "REFERENCE" not in item.GetText():
            continue
        if any(item.GetBoundingBox().Intersects(box) for box in pads):
            item.SetPosition(silk.GetPosition())
            item.SetTextAngle(silk.GetTextAngle())


def build(request: Request) -> Request:
    board = pcbnew.BOARD()
    _apply_rules(board, request["rules"])

    nets: dict[str, Any] = {}
    for name in request["nets"]:
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
        nets[name] = net

    placed: list[tuple[Any, Box]] = []
    unplaced: list[Any] = []
    for item in request["footprints"]:
        footprint = _load_footprint(item["footprint"])
        footprint.SetReference(item["ref"])
        footprint.SetValue(item["value"])
        _clear_fab_reference(footprint)
        board.Add(footprint)
        if item.get("joined_pads"):
            footprint.SetDuplicatePadNumbersAreJumpers(True)
        for pad in footprint.Pads():
            net = item["pads"].get(pad.GetNumber())
            if net is not None:
                pad.SetNet(nets[net])
        if item.get("side") == "bottom":
            footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        if item.get("rotation"):
            footprint.SetOrientationDegrees(item["rotation"])
        if item.get("x") is not None:
            footprint.SetPosition(_point(item["x"], item["y"]))
            placed.append((footprint, _courtyard(footprint)))
        else:
            unplaced.append(footprint)

    for i, (footprint, box) in enumerate(placed):
        for other, other_box in placed[:i]:
            if _overlaps(box, [other_box], 0.0):
                raise DesignProblem(
                    f"hand-placed {footprint.GetReference()} overlaps {other.GetReference()} "
                    "(courtyards)",
                    footprint.GetReference(),
                )

    gap = request["board"]["spacing"]
    if request["board"].get("placer", "connected") == "shelf":
        width = request["board"].get("width") or _target_width(unplaced, gap)
        _auto_place(placed, unplaced, width, gap)
    else:
        bounds: Box | None = None
        if request["board"].get("width") and request["board"].get("height"):
            inset = request["board"]["margin"]
            bounds = (
                inset,
                inset,
                request["board"]["width"] - 2 * inset,
                request["board"]["height"] - 2 * inset,
            )
        _connected_place(placed, unplaced, gap, bounds)

    boxes = [_courtyard(fp) for fp in board.GetFootprints()]
    margin = request["board"]["margin"]
    if request["board"].get("width") and request["board"].get("height"):
        x0, y0 = 0.0, 0.0
        w, h = request["board"]["width"], request["board"]["height"]
    else:
        x0 = min(b[0] for b in boxes) - margin
        y0 = min(b[1] for b in boxes) - margin
        w = max(b[0] + b[2] for b in boxes) + margin - x0
        h = max(b[1] + b[3] for b in boxes) + margin - y0
    outline = pcbnew.PCB_SHAPE(board)
    outline.SetShape(pcbnew.SHAPE_T_RECT)
    outline.SetLayer(pcbnew.Edge_Cuts)
    outline.SetStart(_point(x0, y0))
    outline.SetEnd(_point(x0 + w, y0 + h))
    outline.SetWidth(MM(0.1))
    board.Add(outline)
    _assign_track_widths(board, request["rules"].get("track_widths", {}))

    board.Save(request["pcb"])
    pcbnew.ExportSpecctraDSN(board, request["dsn"])
    settings = board.GetDesignSettings()
    inset = pcbnew.ToMM(settings.m_CopperEdgeClearance) - request["rules"]["clearance"]
    if inset > 0:
        _inset_boundary(request["dsn"], inset)
    _widen_clearances(request["dsn"], ROUTER_MARGIN_UM)
    joined = {item["ref"] for item in request["footprints"] if item.get("joined_pads")}
    if joined:
        _route_one_pad_per_number(request["dsn"], _twin_pads_to_skip(board, joined))
    return {"outline": [x0, y0, w, h], "footprints": _footprint_report(board)}


def _widen_clearances(dsn: str, margin_um: float) -> None:
    """Give the router slightly more clearance than DRC checks: its 45-degree tracks next to
    round pads are rounded to the .dsn's 0.1 um grid and came out 2 um short (0.1979 mm
    for 0.2 mm), which DRC reports as a clearance violation."""
    path = Path(dsn)
    text = path.read_text(encoding="utf-8")
    text = re.sub(
        r"\(clearance ([\d.]+)",
        lambda m: f"(clearance {float(m[1]) + margin_um:g}",
        text,
    )
    path.write_text(text, encoding="utf-8")


def _inset_boundary(dsn: str, inset_mm: float) -> None:
    """Move the router's board boundary in by `inset_mm`. Freerouting keeps only the track
    clearance from the board edge, while KiCad's DRC wants the (larger) copper-to-edge
    clearance; the board outline is a rectangle (see `build`)."""
    path = Path(dsn)
    text = path.read_text(encoding="utf-8")
    match = re.search(r"\(boundary\s*\(path pcb 0((?:\s+-?[\d.]+)+)\s*\)", text)
    if match is None:
        return
    values = [float(v) for v in match[1].split()]
    xs, ys = values[0::2], values[1::2]
    d = inset_mm * 1000  # the .dsn is in um
    x0, x1, y0, y1 = min(xs) + d, max(xs) - d, min(ys) + d, max(ys) - d
    corners = [(x1, y0), (x0, y0), (x0, y1), (x1, y1), (x1, y0)]
    points = "  ".join(f"{x:.1f} {y:.1f}" for x, y in corners)
    text = text[: match.start()] + f"(boundary\n      (path pcb 0  {points})" + text[match.end() :]
    path.write_text(text, encoding="utf-8")


def _twin_pads_to_skip(board: Any, refs: set[str]) -> list[str]:
    """Specctra pin names (`SW1-1@1`) of the twin pads the router need not connect: of the
    pads sharing a number in a joined-pads part, only the one nearest the net's other pads
    is kept (KiCad names them 1, 1@1, 1@2, ... in pad order)."""
    others: dict[str, list[tuple[float, float]]] = {}
    for footprint in board.GetFootprints():
        if footprint.GetReference() in refs:
            continue
        for pad in footprint.Pads():
            if pad.GetNetname():
                at = pad.GetPosition()
                others.setdefault(pad.GetNetname(), []).append((at.x, at.y))
    skip: list[str] = []
    for footprint in board.GetFootprints():
        ref = footprint.GetReference()
        if ref not in refs:
            continue
        twins: dict[str, list[tuple[str, Any]]] = {}
        for pad in footprint.Pads():
            number = pad.GetNumber()
            if not number:
                continue
            group = twins.setdefault(number, [])
            name = number if not group else f"{number}@{len(group)}"
            group.append((name, pad))
        for group in twins.values():
            if len(group) < 2:
                continue
            targets = others.get(group[0][1].GetNetname(), [])

            def distance(
                entry: tuple[str, Any], targets: list[tuple[float, float]] = targets
            ) -> float:
                at = entry[1].GetPosition()
                return min((math.hypot(at.x - x, at.y - y) for x, y in targets), default=0.0)

            keep = min(group, key=distance)[0]
            skip += [f"{ref}-{name}" for name, _ in group if name != keep]
    return skip


def _route_one_pad_per_number(dsn: str, skip: list[str]) -> None:
    """Leave twin pads (`skip`) out of the nets the router must connect: they are joined
    inside the part. Tracks between them only crowd the board, and Freerouting gives up on
    connections next to such pad pairs. They stay in the board as obstacles."""
    path = Path(dsn)
    text = path.read_text(encoding="utf-8")
    for pin in skip:
        text = re.sub(rf' "?{re.escape(pin)}"?(?=[\s)])', "", text)
    path.write_text(text, encoding="utf-8")


def import_ses(request: Request) -> Request:
    board = pcbnew.LoadBoard(request["pcb"])
    if not pcbnew.ImportSpecctraSES(board, request["ses"]):
        raise ValueError("could not import " + request["ses"])
    board.Save(request["pcb"])
    return {"imported": True}


def remove_stubs(request: Request) -> Request:
    """One pass of deleting track stubs with an unconnected end (autorouter leftovers).

    The caller repeats this in fresh processes until nothing is removed: pcbnew's SWIG
    objects become unusable after Remove() or a second LoadBoard() in the same process.
    """
    board = pcbnew.LoadBoard(request["pcb"])
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    stubs = [
        t
        for t in board.GetTracks()
        if t.GetClass() == "PCB_TRACK" and connectivity.TestTrackEndpointDangling(t, False)
    ]
    for track in stubs:
        board.Remove(track)
    if stubs:
        board.Save(request["pcb"])
    return {"removed": len(stubs)}


EDGE_INSET_MM = 0.5  # keep copper fills this far inside the board edge


def pour(request: Request) -> Request:
    """Board-sized zones, one per (net, layer), with thermal reliefs; islands removed."""
    board = pcbnew.LoadBoard(request["pcb"])
    box = board.GetBoardEdgesBoundingBox()
    inset = MM(EDGE_INSET_MM)
    corners = [
        (box.GetX() + inset, box.GetY() + inset),
        (box.GetRight() - inset, box.GetY() + inset),
        (box.GetRight() - inset, box.GetBottom() - inset),
        (box.GetX() + inset, box.GetBottom() - inset),
    ]
    layer_ids = {"top": pcbnew.F_Cu, "bottom": pcbnew.B_Cu}
    zones: list[tuple[Request, str, Any]] = []
    for item in request["pours"]:
        net = board.FindNet(item["net"])
        if net is None:
            raise ValueError("no net " + item["net"] + " on the board")
        for layer in item["layers"]:
            zone = pcbnew.ZONE(board)
            zone.SetLayer(layer_ids[layer])
            zone.SetNet(net)
            zone.Outline().NewOutline()
            for x, y in corners:
                zone.Outline().Append(int(x), int(y))
            zone.SetLocalClearance(MM(item["clearance"]))
            zone.SetMinThickness(MM(item["min_width"]))
            zone.SetThermalReliefGap(MM(max(item["clearance"], 0.5)))
            zone.SetThermalReliefSpokeWidth(MM(max(item["min_width"], 0.5)))
            zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
            zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
            board.Add(zone)
            zones.append((item, layer, zone))
    if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
        raise ValueError("zone fill failed")
    board.Save(request["pcb"])
    return {
        "pours": [
            {
                "net": item["net"],
                "layer": layer,
                "area_mm2": round(pcbnew.ToMM(pcbnew.ToMM(zone.GetFilledArea())), 1),
            }
            for item, layer, zone in zones
        ]
    }


def inspect(request: Request) -> Request:
    board = pcbnew.LoadBoard(request["pcb"])
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    spans: dict[str, list[float]] = {}
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            net = str(pad.GetNetname())
            if not net:
                continue
            x, y = float(pcbnew.ToMM(pad.GetPosition().x)), float(pcbnew.ToMM(pad.GetPosition().y))
            b = spans.setdefault(net, [x, y, x, y])
            b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)
    hpwl = sum((b[2] - b[0]) + (b[3] - b[1]) for b in spans.values())
    return {
        "footprints": _footprint_report(board),
        # placement quality independent of routing: half-perimeter wire length of all nets
        "hpwl_mm": round(hpwl, 3),
        "tracks": len(tracks),
        "vias": len(vias),
        "track_length_mm": round(sum(pcbnew.ToMM(t.GetLength()) for t in tracks), 3),
        "unrouted": connectivity.GetUnconnectedCount(False),
    }


def _footprint_report(board: Any) -> Request:
    report: Request = {}
    for footprint in board.GetFootprints():
        pos = footprint.GetPosition()
        x, y, w, h = _courtyard(footprint)
        report[footprint.GetReference()] = {
            "x": round(pcbnew.ToMM(pos.x), 3),
            "y": round(pcbnew.ToMM(pos.y), 3),
            "rotation": footprint.GetOrientationDegrees(),
            "side": "bottom" if footprint.IsFlipped() else "top",
            "box": [round(x, 3), round(y, 3), round(w, 3), round(h, 3)],
            **_holes(footprint),
        }
    return report


def _holes(footprint: Any) -> Request:
    """Smallest plated drill, its copper ring, and smallest non-plated drill, in mm."""
    plated: list[tuple[float, float]] = []
    npth: list[float] = []
    for pad in footprint.Pads():
        drill = pad.GetDrillSize()
        hole = float(pcbnew.ToMM(min(drill.x, drill.y)))
        if hole <= 0:
            continue
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
            npth.append(hole)
        elif pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
            size = pad.GetSize()
            copper = float(pcbnew.ToMM(min(size.x, size.y)))
            plated.append((hole, (copper - hole) / 2))
    holes: Request = {}
    if plated:
        holes["drill_mm"] = round(min(h for h, _ in plated), 3)
        holes["ring_mm"] = round(min(r for _, r in plated), 3)
    if npth:
        holes["npth_mm"] = round(min(npth), 3)
    return holes


def main() -> None:
    request = json.load(sys.stdin)
    commands = {
        "build": build,
        "import_ses": import_ses,
        "remove_stubs": remove_stubs,
        "pour": pour,
        "inspect": inspect,
    }
    result: Request
    try:
        result = {"ok": True, "result": commands[request["command"]](request)}
    except DesignProblem as exc:
        result = {"ok": False, "error": str(exc), "design": True, "ref": exc.ref}
    except Exception as exc:  # report every failure as JSON for the caller
        result = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
    sys.stdout.write("\n@@EDA-JSON@@" + json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
