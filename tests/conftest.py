import shutil
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

import pytest

LED_CIRCUIT = """
parts:
  V1: { type: dc_voltage_source, voltage: 5V }
  R1: { type: resistor, resistance: 330ohm }
  D1: { type: led, color: red }
nets:
  VCC: [V1.+, R1.1]
  N1: [R1.2, D1.anode]
  GND: [D1.cathode, V1.-]
"""

LED_TESTS = """
tests:
  led_current:
    analysis: { type: op }
    expect:
      - { measure: current(D1), min: 9mA, max: 10mA }
"""

type MakeProject = Callable[..., Path]


@pytest.fixture
def make_project(tmp_path: Path) -> MakeProject:
    def make(circuit: str = LED_CIRCUIT, tests: str = LED_TESTS, name: str = "demo") -> Path:
        root = tmp_path / name
        (root / "design").mkdir(parents=True)
        (root / "project.yaml").write_text(f"name: {name}\n", encoding="utf-8")
        (root / "design" / "circuit.yaml").write_text(dedent(circuit).lstrip(), encoding="utf-8")
        (root / "design" / "test.yaml").write_text(dedent(tests).lstrip(), encoding="utf-8")
        return root

    return make


requires_ngspice = pytest.mark.skipif(shutil.which("ngspice") is None, reason="needs ngspice")
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
requires_kicad = pytest.mark.skipif(
    shutil.which("kicad-cli") is None or shutil.which("java") is None,
    reason="needs kicad-cli, KiCad's Python, and Java",
)
