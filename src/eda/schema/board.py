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


class BoardFile(StrictModel):
    """`design/board.yaml`."""

    board: Outline = Outline()
    rules: Rules = Rules()
    placement: dict[Ident, Placement] = Field(default_factory=dict[Ident, Placement])
    routing: Routing = Routing()
    pours: list[Pour] = Field(default_factory=list[Pour])
