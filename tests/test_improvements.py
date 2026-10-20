import json

import pytest

from eda import eseries, history, pipeline
from eda.design import load_design
from eda.project import ProjectPaths
from eda.render.schematic import label_nets
from eda.result import Diagnostics
from eda.summary import CaseSummary, DesignSummary, PartSummary, diff
from eda.units import parse_any_quantity
from tests.conftest import LED_CIRCUIT, MakeProject, requires_ngspice


def test_eseries_tables() -> None:
    assert len(eseries.mantissas("E24")) == 24
    assert len(eseries.mantissas("E96")) == 96
    assert 3.16 in eseries.mantissas("E96")
    assert 9.20 in eseries.mantissas("E192")


@pytest.mark.parametrize(
    ("target", "series", "nearest", "below", "above"),
    [
        (315.0, "E96", 316.0, 309.0, 316.0),
        (315.0, "E24", 330.0, 300.0, 330.0),
        (1000.0, "E12", 1000.0, 1000.0, 1000.0),
        (9.9e-6, "E6", 10e-6, 6.8e-6, 10e-6),
        (0.95, "E12", 1.0, 0.82, 1.0),
    ],
)
def test_eseries_nearest(
    target: float, series: str, nearest: float, below: float, above: float
) -> None:
    pick = eseries.nearest(target, series)
    assert (pick.nearest, pick.below, pick.above) == pytest.approx((nearest, below, above))


def test_parse_any_quantity() -> None:
    assert parse_any_quantity("4.7kohm") == (4700.0, "ohm")
    assert parse_any_quantity("10uF") == pytest.approx((1e-5, "F"))


def _summary(resistance: str, extra_net: bool = False) -> DesignSummary:
    nets = {"VCC": ["V1.+", "R1.1"], "GND": ["R1.2", "V1.-"]}
    if extra_net:
        nets["X"] = ["R1.2"]
    return DesignSummary(
        parts={"R1": PartSummary(type="resistor", params={"resistance": resistance})},
        nets=nets,
        tests={"t": CaseSummary(analysis="op", expectations=["voltage(VCC): <= 5V"])},
    )


def test_summary_diff() -> None:
    assert diff(_summary("470Ω"), _summary("470Ω")) == []
    assert diff(_summary("470Ω"), _summary("316Ω")) == ["R1.resistance: 470Ω → 316Ω"]
    assert "added net X: R1.2" in diff(_summary("1kΩ"), _summary("1kΩ", extra_net=True))


def test_label_nets(make_project: MakeProject) -> None:
    design = load_design(make_project(), Diagnostics())
    assert design is not None
    # Bits follow net order: VCC=2, N1=3, GND=4. Only N1 gets a text label.
    svg = (
        '<svg><line x1="0" x2="0" y1="0" y2="10" class="net_2"/>'
        '<line x1="5" x2="25" y1="40" y2="40" class="net_3"/>'
        '<line x1="5" x2="5" y1="40" y2="90" class="net_3"/>'
        '<line x1="0" x2="9" y1="99" y2="99" class="net_4"/></svg>'
    )
    out = label_nets(svg, design.circuit)
    assert out.count('class="netlabel"') == 1
    assert '<text x="15" y="37" text-anchor="middle" class="netlabel">N1</text>' in out


@requires_ngspice
def test_log_records_semantic_changes(make_project: MakeProject) -> None:
    root = make_project()
    paths = ProjectPaths(root)
    history.record_run(paths, pipeline.run(root))
    circuit = paths.circuit_file
    circuit.write_text(LED_CIRCUIT.replace("330ohm", "316ohm").lstrip(), encoding="utf-8")
    history.record_run(paths, pipeline.run(root), note="Fix: E96 value")
    entries = [json.loads(line) for line in paths.log_file.read_text().splitlines()]
    last = entries[-1]
    assert last["note"] == "Fix: E96 value"
    assert last["design_changes"] == ["R1.resistance: 330Ω → 316Ω"]
    assert last["changed"] == ["design/circuit.yaml"]
    assert "+" in last["time"] or "-" in last["time"][10:]  # local offset present
