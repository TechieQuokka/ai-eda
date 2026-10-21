"""Electrical rule checks on a resolved circuit."""

from eda.model import GROUND, ResolvedCircuit
from eda.result import Diagnostics


def run_erc(circuit: ResolvedCircuit, diag: Diagnostics) -> None:
    src = circuit.source
    connected: set[tuple[str, str]] = {pin for pins in circuit.nets.values() for pin in pins}

    for ref, part in circuit.parts.items():
        optional = set(part.spec.optional_pins)
        missing = [
            pin for pin in part.spec.pins if (ref, pin) not in connected and pin not in optional
        ]
        if missing:
            diag.error(
                "unconnected_pin",
                f"{ref}: pin(s) {', '.join(missing)} not connected to any net",
                src.at("parts", ref),
                pins=missing,
            )
        nets = {circuit.net_of(ref, pin) for pin in part.spec.pins} - {None}
        if len(part.spec.pins) > 1 and len(nets) == 1 and not missing and not optional:
            rule, message = "shorted_part", f"{ref}: all pins are on net {nets.pop()!r}"
            if part.spec.category == "source":
                diag.error(rule, f"{message} (source shorted)", src.at("parts", ref))
            else:
                diag.warning(rule, message, src.at("parts", ref))

    for net, pins in circuit.nets.items():
        if len(pins) == 1:
            ref, pin = pins[0]
            diag.error(
                "single_pin_net",
                f"net {net!r} only connects {ref}.{pin}; a net needs at least two pins",
                src.at("nets", net),
            )

    if GROUND not in circuit.nets:
        diag.error(
            "missing_ground",
            f"no net named {GROUND!r}; one net must be the {GROUND} reference",
            src.at("nets"),
        )
        return

    _check_dc_loops(circuit, diag)

    # Union nets joined by parts that conduct at DC; anything not reaching GND floats.
    parent = {net: net for net in circuit.nets}

    def find(net: str) -> str:
        while parent[net] != net:
            parent[net] = parent[parent[net]]
            net = parent[net]
        return net

    for ref, part in circuit.parts.items():
        if not part.spec.dc_path:
            continue
        nets = [n for pin in part.spec.pins if (n := circuit.net_of(ref, pin)) is not None]
        for other in nets[1:]:
            parent[find(other)] = find(nets[0])

    ground = find(GROUND)
    for net in circuit.nets:
        if find(net) != ground:
            diag.error(
                "no_dc_path",
                f"net {net!r} has no DC path to {GROUND} (e.g. only capacitors); "
                "the simulator cannot solve its operating point",
                src.at("nets", net),
            )


# SPICE element letters of two-terminal parts with no resistance at DC.
_ZERO_OHM_AT_DC = {"V", "L"}


def _check_dc_loops(circuit: ResolvedCircuit, diag: Diagnostics) -> None:
    """A loop made only of voltage sources and inductors is a short circuit at DC: the
    simulator either fails or returns a meaningless current (singular matrix)."""
    parent = {net: net for net in circuit.nets}
    members: dict[str, list[str]] = {net: [] for net in circuit.nets}

    def find(net: str) -> str:
        while parent[net] != net:
            parent[net] = parent[parent[net]]
            net = parent[net]
        return net

    for ref, part in circuit.parts.items():
        spice = part.spec.spice
        if spice.prefix not in _ZERO_OHM_AT_DC or spice.generator or len(part.spec.pins) != 2:
            continue
        a, b = (circuit.net_of(ref, pin) for pin in part.spec.pins)
        if a is None or b is None or a == b:
            continue  # unconnected or shorted parts are reported above
        root_a, root_b = find(a), find(b)
        if root_a == root_b:
            loop = [*members[root_a], ref]
            diag.error(
                "dc_loop",
                f"{ref} closes a loop of voltage sources and inductors ({', '.join(loop)}) "
                f"between nets {a!r} and {b!r}: at DC that is a short circuit",
                circuit.source.at("parts", ref),
                parts=loop,
            )
            continue
        parent[root_b] = root_a
        members[root_a] += [*members[root_b], ref]
