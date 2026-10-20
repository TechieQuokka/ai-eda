"""Resolved circuit → netlistsvg (yosys-style JSON) for automatic schematic layout."""

import re
from html import escape
from pathlib import Path

from pydantic import JsonValue

from eda.model import GROUND, ResolvedCircuit, ResolvedPart

_LABEL = re.compile(r"<param:([^>]+)>")
_PAD = {"left": 10, "top": 10, "right": 60, "bottom": 10}
_LINE = re.compile(
    r'<line x1="([-\d.]+)" x2="([-\d.]+)" y1="([-\d.]+)" y2="([-\d.]+)" class="net_(\d+)"/>'
)

SKIN = Path(__file__).with_name("skin.svg")


def net_bits(circuit: ResolvedCircuit) -> dict[str, int]:
    """netlistsvg identifies nets by integer "bits"; 0 and 1 are reserved constants."""
    return {net: index + 2 for index, net in enumerate(circuit.nets)}


def supply_nets(circuit: ResolvedCircuit) -> list[str]:
    """Nets driven by a source whose other terminal is GND; drawn with a supply symbol."""
    found: list[str] = []
    for ref, part in circuit.parts.items():
        if part.spec.category != "source":
            continue
        plus = circuit.net_of(ref, part.spec.pins[0])
        minus = circuit.net_of(ref, part.spec.pins[1])
        if plus and minus == GROUND and plus != GROUND and plus not in found:
            found.append(plus)
    return found


def label(part: ResolvedPart) -> str:
    return _LABEL.sub(lambda m: part.labels.get(m[1], "?"), part.spec.symbol.label)


def to_netlistsvg(name: str, circuit: ResolvedCircuit) -> dict[str, JsonValue]:
    bits = net_bits(circuit)
    cells: dict[str, JsonValue] = {}

    for ref, part in circuit.parts.items():
        directions: dict[str, JsonValue] = {}
        connections: dict[str, JsonValue] = {}
        for index, pin in enumerate(part.spec.pins):
            port = part.spec.symbol.ports[pin]
            # Layout runs top-down: inputs on top. Sources put + on top via "output".
            first = index == 0
            is_source = part.spec.category == "source"
            directions[port] = "output" if first == is_source else "input"
            net = circuit.net_of(ref, pin)
            if net is not None:
                connections[port] = [bits[net]]
        cells[ref] = {
            "type": part.spec.symbol.cell,
            "port_directions": directions,
            "connections": connections,
            "attributes": {"value": label(part)},
        }

    if GROUND in bits:
        cells["__gnd"] = {
            "type": "gnd",
            "port_directions": {"A": "input"},
            "connections": {"A": [bits[GROUND]]},
            "attributes": {"name": GROUND},
        }
    for net in supply_nets(circuit):
        cells[f"__supply_{net}"] = {
            "type": "vcc",
            "port_directions": {"A": "output"},
            "connections": {"A": [bits[net]]},
            "attributes": {"name": net},
        }

    netnames: dict[str, JsonValue] = {net: {"bits": [bit]} for net, bit in bits.items()}
    return {"modules": {name: {"ports": {}, "cells": cells, "netnames": netnames}}}


def pad_svg(svg: str) -> str:
    """netlistsvg sizes the canvas without room for labels on the right; add margins."""
    match = re.search(r'<svg\b[^>]*\bwidth="([\d.]+)"[^>]*\bheight="([\d.]+)"[^>]*>', svg)
    if match is None or "viewBox" in match.group(0):
        return svg
    width = float(match.group(1)) + _PAD["left"] + _PAD["right"]
    height = float(match.group(2)) + _PAD["top"] + _PAD["bottom"]
    tag = match.group(0)
    new_tag = re.sub(r'\bwidth="[\d.]+"', f'width="{width:g}"', tag)
    new_tag = re.sub(r'\bheight="[\d.]+"', f'height="{height:g}"', new_tag)
    new_tag = new_tag[:-1] + (f' viewBox="{-_PAD["left"]} {-_PAD["top"]} {width:g} {height:g}">')
    background = (
        f'<rect x="{-_PAD["left"]}" y="{-_PAD["top"]}" width="{width:g}" height="{height:g}" '
        'fill="#fff" stroke="none"/>'
    )
    return svg.replace(tag, new_tag + background, 1)


def label_nets(svg: str, circuit: ResolvedCircuit) -> str:
    """Write each net's name next to its longest wire segment.

    GND and supply nets already carry a symbol with their name, so they are skipped.
    """
    skip = {GROUND, *supply_nets(circuit)}
    names = {bit: net for net, bit in net_bits(circuit).items() if net not in skip}
    segments: dict[int, list[tuple[float, float, float, float]]] = {}
    for match in _LINE.finditer(svg):
        x1, x2, y1, y2 = (float(match[i]) for i in range(1, 5))
        segments.setdefault(int(match[5]), []).append((x1, y1, x2, y2))

    labels: list[str] = []
    for bit, net in names.items():
        lines = segments.get(bit)
        if not lines:
            continue
        # Prefer horizontal runs: a label above them never collides with vertical parts.
        x1, y1, x2, y2 = max(
            lines, key=lambda s: (s[1] == s[3], abs(s[2] - s[0]) + abs(s[3] - s[1]))
        )
        if y1 == y2:
            x, y, anchor = (x1 + x2) / 2, y1 - 3, "middle"
        else:
            x, y, anchor = x1 - 4, (y1 + y2) / 2 + 3, "end"
        labels.append(
            f'<text x="{x:g}" y="{y:g}" text-anchor="{anchor}" class="netlabel">'
            f"{escape(net)}</text>"
        )
    if not labels:
        return svg
    return svg.replace("</svg>", "\n".join(labels) + "\n</svg>", 1)
