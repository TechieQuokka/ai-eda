"""Resolved circuit → netlistsvg (yosys-style JSON) for automatic schematic layout."""

import re

from pydantic import JsonValue

from eda.model import GROUND, ResolvedCircuit, ResolvedPart

_LABEL = re.compile(r"<param:([^>]+)>")
_PAD = {"left": 10, "top": 10, "right": 60, "bottom": 10}


def label(part: ResolvedPart) -> str:
    return _LABEL.sub(lambda m: part.labels.get(m[1], "?"), part.spec.symbol.label)


def to_netlistsvg(name: str, circuit: ResolvedCircuit) -> dict[str, JsonValue]:
    bits = {net: index + 2 for index, net in enumerate(circuit.nets)}
    cells: dict[str, JsonValue] = {}
    supply_nets: list[str] = []

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
        if part.spec.category == "source":
            plus = circuit.net_of(ref, part.spec.pins[0])
            minus = circuit.net_of(ref, part.spec.pins[1])
            if plus and minus == GROUND and plus != GROUND and plus not in supply_nets:
                supply_nets.append(plus)

    if GROUND in bits:
        cells["__gnd"] = {
            "type": "gnd",
            "port_directions": {"A": "input"},
            "connections": {"A": [bits[GROUND]]},
            "attributes": {"name": GROUND},
        }
    for net in supply_nets:
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
