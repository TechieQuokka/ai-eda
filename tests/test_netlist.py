from pathlib import Path

from eda.adapters.ngspice import parse_raw
from eda.design import load_design
from eda.model import Op, Tran
from eda.netlist.spice import build_netlist, current_vector
from eda.result import Diagnostics
from tests.conftest import MakeProject


def test_netlist_lines(make_project: MakeProject) -> None:
    design = load_design(make_project(), Diagnostics())
    assert design is not None
    text = build_netlist("demo", design.circuit, Op(), Path("t.raw"))
    lines = text.splitlines()
    # Every two-terminal part's first pin goes through a 0 V sense source.
    assert "v_s_v1_0 vcc s_v1_0 DC 0" in lines
    assert "V1 s_v1_0 0 DC 5 AC 0" in lines
    assert "R1 s_r1_0 n1 330" in lines
    assert "D1 s_d1_0 0 led_red" in lines
    assert any(line.startswith(".model led_red D(IS=3.568e-18") for line in lines)
    assert ".option savecurrents" not in lines
    assert "op" in lines
    assert "write t.raw all" in lines


def test_tran_command(make_project: MakeProject) -> None:
    design = load_design(make_project(), Diagnostics())
    assert design is not None
    text = build_netlist("demo", design.circuit, Tran(1e-5, 5e-3, None), Path("t.raw"))
    assert "tran 1e-05 0.005" in text.splitlines()


def test_current_vectors(make_project: MakeProject) -> None:
    design = load_design(make_project(), Diagnostics())
    assert design is not None
    parts = design.circuit.parts
    assert current_vector(parts["V1"]) == "i(v_s_v1_0)"
    assert current_vector(parts["R1"]) == "i(v_s_r1_0)"
    assert current_vector(parts["D1"]) == "i(v_s_d1_0)"


RAW = """Title: * t
Date: x
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tv(out)\tvoltage
Values:
 0\t0.0e+00
\t1.0e+00
 1\t1.0e-03
\t2.5e+00
"""


def test_parse_raw() -> None:
    (plot,) = parse_raw(RAW)
    assert plot.name == "Transient Analysis"
    assert plot.vectors == {"time": [0.0, 1e-3], "v(out)": [1.0, 2.5]}
