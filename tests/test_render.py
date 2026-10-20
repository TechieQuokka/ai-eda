import pytest

from eda.design import load_design
from eda.render.schematic import pad_svg, to_netlistsvg
from eda.render.waveform import Bound, Trace, nice_ticks, plot
from eda.result import Diagnostics
from tests.conftest import MakeProject


def test_netlistsvg_json(make_project: MakeProject) -> None:
    design = load_design(make_project(), Diagnostics())
    assert design is not None
    data = to_netlistsvg("demo", design.circuit)
    module = data["modules"]
    assert isinstance(module, dict)
    cells = module["demo"]
    assert isinstance(cells, dict)
    cell_map = cells["cells"]
    assert isinstance(cell_map, dict)
    assert {"V1", "R1", "D1", "__gnd", "__supply_VCC"} <= set(cell_map)
    r1 = cell_map["R1"]
    assert isinstance(r1, dict)
    assert r1["type"] == "r_v"
    assert r1["attributes"] == {"value": "330Ω"}


def test_pad_svg_adds_viewbox() -> None:
    padded = pad_svg('<svg xmlns="x" width="100" height="50"><g/></svg>')
    assert 'viewBox="-10 -10 170 70"' in padded
    assert 'width="170"' in padded


def test_nice_ticks() -> None:
    assert nice_ticks(0, 1) == pytest.approx([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ticks = nice_ticks(0.0013, 0.0047)
    assert ticks[0] <= 0.0013 and ticks[-1] >= 0.0047
    assert nice_ticks(5, 5)


def test_plot_svg() -> None:
    svg = plot(
        "t",
        [0.0, 1e-3, 2e-3],
        [Trace("voltage(OUT)", "V", [0.0, 2.0, 4.0]), Trace("current(R1)", "A", [1e-3, 0, 0])],
        [Bound("final(voltage(OUT)) min", "V", 3.5)],
    )
    assert svg.startswith("<svg")
    assert svg.count("<polyline") == 2
    assert "stroke-dasharray" in svg
