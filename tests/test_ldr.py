from dataclasses import replace

import pytest

from eda import pipeline
from eda.design import load_design
from eda.netlist.spice import alter_commands
from eda.result import Diagnostics
from tests.conftest import MakeProject, requires_ngspice

DIVIDER = """
parts:
  V1: { type: dc_voltage_source, voltage: 2V }
  R1: { type: resistor, resistance: 15kohm }
  LDR1: { type: ldr, light: 10lx }
nets:
  VCC: [V1.+, R1.1]
  OUT: [R1.2, LDR1.1]
  GND: [V1.-, LDR1.2]
"""


@requires_ngspice
@pytest.mark.parametrize(
    ("light", "ohms"),
    [("10lx", 15e3), ("1lx", 15e3 * 10**0.7), ("100lx", 15e3 * 10**-0.7), ("0lx", 1e6)],
)
def test_ldr_resistance(make_project: MakeProject, light: str, ohms: float) -> None:
    # 15 kΩ over the LDR: OUT = 2 V * R / (15 kΩ + R).
    out = 2 * ohms / (15e3 + ohms)
    tests = f"""
    tests:
      t:
        analysis: {{ type: op }}
        parts: {{ LDR1: {{ light: {light} }} }}
        expect:
          - {{ measure: voltage(OUT), min: {out * 0.999}V, max: {out * 1.001}V }}
    """
    result = pipeline.sim(make_project(DIVIDER, tests))
    assert result.ok, result.errors


def test_light_can_change_during_a_run(make_project: MakeProject) -> None:
    diag = Diagnostics()
    design = load_design(make_project(DIVIDER, "tests: {}\n"), diag)
    assert design is not None
    old = design.circuit.parts["LDR1"]
    new = replace(old, values={**old.values, "light": 1.0})
    (command,) = alter_commands(old, new, design.circuit)
    assert command.startswith("alter ")
    assert float(command.split("=")[1]) == pytest.approx(15e3 * 10**0.7, rel=1e-9)
