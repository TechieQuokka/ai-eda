"""PCB house capabilities: the limits a board must stay within to be made (DESIGN_DECISIONS.md §23).

`board.yaml` names a house (`fab: jlcpcb`) or gives its own limits; the rules are checked
when the design loads, the built board (size, footprint holes) after it is built.
"""

from dataclasses import dataclass

from eda.result import Diagnostics, Location


@dataclass(frozen=True)
class Fab:
    """Minimums of a two-layer, 1 oz board at the house's standard price; lengths in mm."""

    name: str
    min_track: float
    min_clearance: float
    min_drill: float  # plated holes: vias and through-hole pads
    min_via_ring: float  # (via diameter - via drill) / 2
    min_pad_ring: float  # copper around a through-hole pad's hole
    min_npth_drill: float  # non-plated (mounting) holes
    min_size: float  # shortest board side
    max_width: float  # longest board side
    max_height: float  # the other side


# https://jlcpcb.com/capabilities/pcb-capabilities, read 2026-10-21 (1-2 layers, 1 oz).
PRESETS: dict[str, Fab] = {
    "jlcpcb": Fab(
        name="jlcpcb",
        min_track=0.10,
        min_clearance=0.10,
        min_drill=0.15,
        min_via_ring=0.05,  # via diameter >= hole + 0.1 mm
        min_pad_ring=0.18,  # absolute minimum (0.25 mm recommended)
        min_npth_drill=0.50,
        min_size=3.0,
        max_width=670.0,
        max_height=600.0,
    ),
}

FIELDS = (
    "min_track",
    "min_clearance",
    "min_drill",
    "min_via_ring",
    "min_pad_ring",
    "min_npth_drill",
    "min_size",
    "max_width",
    "max_height",
)


def below(
    fab: Fab,
    diag: Diagnostics,
    what: str,
    value: float,
    limit: str,
    at: Location | None = None,
    **details: object,
) -> bool:
    """Record a `fab_limit` error if `value` (mm) is under the house's `limit`."""
    minimum = float(getattr(fab, limit))
    if value >= minimum - 1e-9:
        return False
    diag.error(
        "fab_limit",
        f"{what} is {value:g} mm, below {fab.name}'s {limit.replace('_', ' ')} of {minimum:g} mm",
        at,
        fab=fab.name,
        limit=limit,
        value_mm=round(value, 4),
        minimum_mm=minimum,
        **details,
    )
    return True


def check_size(fab: Fab, width: float, height: float, diag: Diagnostics, at: Location) -> None:
    """The board must fit the house's panel in one of its two orientations."""
    short, long = sorted((width, height))
    below(fab, diag, "the board's shorter side", short, "min_size", at)
    limit_short, limit_long = sorted((fab.max_width, fab.max_height))
    if long > limit_long + 1e-9 or short > limit_short + 1e-9:
        diag.error(
            "fab_limit",
            f"the board is {width:g} x {height:g} mm, larger than {fab.name}'s "
            f"{fab.max_width:g} x {fab.max_height:g} mm",
            at,
            fab=fab.name,
            limit="max_size",
            size_mm=[round(width, 3), round(height, 3)],
            maximum_mm=[fab.max_width, fab.max_height],
        )
