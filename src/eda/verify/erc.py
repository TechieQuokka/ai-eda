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
