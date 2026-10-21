"""Compact design summary (what `eda check` reports) and a semantic diff between two."""

from pydantic import BaseModel

from eda.model import Design, domain_of


class PartSummary(BaseModel):
    type: str
    params: dict[str, str]


class CaseSummary(BaseModel):
    analysis: str
    expectations: list[str]


class DesignSummary(BaseModel):
    parts: dict[str, PartSummary]
    nets: dict[str, list[str]]
    tests: dict[str, CaseSummary]


def summarize(design: Design) -> DesignSummary:
    return DesignSummary(
        parts={
            ref: PartSummary(type=part.spec.id, params=dict(part.labels))
            for ref, part in design.circuit.parts.items()
        },
        nets={
            net: [f"{ref}.{pin}" for ref, pin in pins] for net, pins in design.circuit.nets.items()
        },
        tests={
            name: CaseSummary(
                analysis=domain_of(test.analysis),
                expectations=[f"{e.measure.text}: {e.range_text}" for e in test.expectations],
            )
            for name, test in design.tests.items()
        },
    )


def diff(old: DesignSummary, new: DesignSummary) -> list[str]:
    """Human-readable changes, e.g. `R1.resistance: 470Ω → 316Ω`."""
    changes: list[str] = []
    for ref in old.parts.keys() - new.parts.keys():
        changes.append(f"removed part {ref} ({old.parts[ref].type})")
    for ref, part in new.parts.items():
        before = old.parts.get(ref)
        params = ", ".join(f"{k}={v}" for k, v in part.params.items())
        if before is None:
            changes.append(f"added part {ref} ({part.type}{', ' + params if params else ''})")
            continue
        if before.type != part.type:
            changes.append(f"{ref}: type {before.type} → {part.type}")
            continue
        for key in sorted(before.params.keys() | part.params.keys()):
            a, b = before.params.get(key), part.params.get(key)
            if a != b:
                changes.append(f"{ref}.{key}: {a} → {b}")

    for net in old.nets.keys() - new.nets.keys():
        changes.append(f"removed net {net}")
    for net, pins in new.nets.items():
        before = old.nets.get(net)
        if before is None:
            changes.append(f"added net {net}: {', '.join(pins)}")
            continue
        added = [p for p in pins if p not in before]
        removed = [p for p in before if p not in pins]
        if added or removed:
            parts = [f"+{p}" for p in added] + [f"-{p}" for p in removed]
            changes.append(f"net {net}: {' '.join(parts)}")

    for name in old.tests.keys() - new.tests.keys():
        changes.append(f"removed test {name}")
    for name, test in new.tests.items():
        before = old.tests.get(name)
        if before is None:
            changes.append(f"added test {name} ({test.analysis})")
        elif before != test:
            changes.append(f"test {name}: {'; '.join(test.expectations)}")
    return sorted(changes, key=lambda c: (c.startswith(("added", "removed")), c))
