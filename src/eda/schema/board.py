from typing import Literal

from pydantic import Field

from eda.schema.common import Ident, StrictModel


class Outline(StrictModel):
    width: str | None = None  # e.g. 50mm; both omitted → fit to the placed parts
    height: str | None = None
    margin: str = "3mm"
    spacing: str = "3mm"  # gap between auto-placed parts
    # connected: keep connected parts together (short tracks); shelf: pack by size
    placer: Literal["connected", "shelf"] = "connected"
    layers: Literal[2] = 2


class Rules(StrictModel):
    clearance: str = "0.2mm"
    track_width: str = "0.4mm"
    via_diameter: str = "0.8mm"
    via_drill: str = "0.4mm"


class Placement(StrictModel):
    x: str
    y: str
    rotation: str = "0deg"
    side: Literal["top", "bottom"] = "top"


class Routing(StrictModel):
    passes: int = Field(default=20, ge=1, le=500)


class Pour(StrictModel):
    """A copper fill over the whole board connected to one net (usually GND)."""

    net: Ident
    layer: Literal["top", "bottom", "both"] = "bottom"
    clearance: str = "0.3mm"  # gap to other nets' copper
    min_width: str = "0.25mm"  # thinnest copper the fill keeps


class FabSpec(StrictModel):
    """A PCB house's limits: a preset (`base`) with overrides, or limits of its own.
    Limits left out come from `base`, or are not checked."""

    base: str | None = None  # e.g. jlcpcb
    name: str | None = None
    min_track: str | None = None
    min_clearance: str | None = None
    min_drill: str | None = None
    min_via_ring: str | None = None
    min_pad_ring: str | None = None
    min_npth_drill: str | None = None
    min_size: str | None = None
    max_width: str | None = None
    max_height: str | None = None


class BoardFile(StrictModel):
    """`design/board.yaml`."""

    board: Outline = Outline()
    rules: Rules = Rules()
    placement: dict[Ident, Placement] = Field(default_factory=dict[Ident, Placement])
    routing: Routing = Routing()
    pours: list[Pour] = Field(default_factory=list[Pour])
    fab: str | FabSpec | None = None  # PCB house whose limits the board must meet
