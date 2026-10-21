import pytest

from eda import pipeline
from tests.conftest import MakeProject, requires_ngspice

SWITCH = """
parts:
  V1: { type: dc_voltage_source, voltage: 5V }
  Q1: { type: npn, model: MODEL }
  RB: { type: resistor, resistance: 10kohm }
  RC: { type: resistor, resistance: 470ohm }
nets:
  VCC: [V1.+, RB.1, RC.1]
  BASE: [RB.2, Q1.b]
  COL: [RC.2, Q1.c]
  GND: [V1.-, Q1.e]
"""

TESTS = """
tests:
  on:
    analysis: { type: op }
    expect:
      - { measure: voltage(COL), max: 0.2V }
      - { measure: voltage(BASE), min: 0.6V, max: 0.85V }
      - { measure: current(Q1.c), min: 9mA, max: 11mA }
      - { measure: current(Q1.b), min: 0.4mA, max: 0.45mA }
"""


@requires_ngspice
@pytest.mark.parametrize("model", ["2N3904", "PN2222A"])
def test_npn_switch_saturates(make_project: MakeProject, model: str) -> None:
    # 0.43 mA of base current saturates the transistor with ~10 mA in the collector.
    result = pipeline.sim(make_project(SWITCH.replace("MODEL", model), TESTS))
    assert result.ok, result.errors
