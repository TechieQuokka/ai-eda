from pathlib import Path

import pytest

from eda.design import load_design
from eda.result import Diagnostics
from tests.conftest import MakeProject


def _rules(root: Path) -> list[str]:
    diag = Diagnostics()
    load_design(root, diag)
    return [e.rule for e in diag.errors]


def test_valid_project_resolves(make_project: MakeProject) -> None:
    diag = Diagnostics()
    design = load_design(make_project(), diag)
    assert diag.ok, diag.errors
    assert design is not None
    r1 = design.circuit.parts["R1"]
    assert r1.values["resistance"] == 330.0
    assert r1.labels["resistance"] == "330Ω"
    assert r1.spice_name == "R1"
    assert design.circuit.net_of("D1", "anode") == "N1"
    assert set(design.tests) == {"led_current"}


def test_spice_name_gets_prefix(make_project: MakeProject) -> None:
    circuit = """
    parts:
      V1: { type: dc_voltage_source, voltage: 5V }
      LOAD: { type: resistor, resistance: 1kohm }
    nets:
      A: [V1.+, LOAD.1]
      GND: [LOAD.2, V1.-]
    """
    design = load_design(make_project(circuit, "tests: {}\n"), Diagnostics())
    assert design is not None
    assert design.circuit.parts["LOAD"].spice_name == "R_LOAD"


@pytest.mark.parametrize(
    ("circuit", "rule"),
    [
        ("parts:\n  R1: { type: resistor }\nnets: {}\n", "missing_param"),
        ("parts:\n  R1: { type: resistor, resistance: 1k }\nnets: {}\n", "invalid_value"),
        ("parts:\n  R1: { type: nope }\nnets: {}\n", "unknown_part_type"),
        ("parts:\n  R1: { type: resistor, resistance: 1kohm, x: 1 }\nnets: {}\n", "unknown_param"),
        ("parts:\n  D1: { type: led, color: pink }\nnets: {}\n", "invalid_choice"),
        ("parts: {}\nnets:\n  A: [R9.1]\n", "unknown_part"),
        (
            "parts:\n  R1: { type: resistor, resistance: 1kohm }\n  r1: { type: resistor, "
            "resistance: 1kohm }\nnets: {}\n",
            "case_collision",
        ),
        ("parts: {}\nnets:\n  Gnd: []\n", "schema"),
    ],
)
def test_circuit_errors(make_project: MakeProject, circuit: str, rule: str) -> None:
    assert rule in _rules(make_project(circuit, "tests: {}\n"))


def test_erc_rules(make_project: MakeProject) -> None:
    circuit = """
    parts:
      V1: { type: dc_voltage_source, voltage: 5V }
      R1: { type: resistor, resistance: 1kohm }
      R2: { type: resistor, resistance: 1kohm }
      C1: { type: capacitor, capacitance: 1uF }
      C2: { type: capacitor, capacitance: 1uF }
    nets:
      VCC: [V1.+, R1.1]
      MID: [R1.2, C1.1]
      ISLAND: [C1.2, C2.1]
      LONE: [R2.1]
      GND: [V1.-, C2.2]
    """
    rules = _rules(make_project(circuit, "tests: {}\n"))
    assert "unconnected_pin" in rules  # R2.2
    assert "single_pin_net" in rules  # LONE
    assert "no_dc_path" in rules  # MID / ISLAND only reach GND through capacitors


def test_missing_ground(make_project: MakeProject) -> None:
    circuit = """
    parts:
      V1: { type: dc_voltage_source, voltage: 5V }
      R1: { type: resistor, resistance: 1kohm }
    nets:
      A: [V1.+, R1.1]
      B: [R1.2, V1.-]
    """
    assert "missing_ground" in _rules(make_project(circuit, "tests: {}\n"))


def test_short_between_nets(make_project: MakeProject) -> None:
    circuit = """
    parts:
      V1: { type: dc_voltage_source, voltage: 5V }
      R1: { type: resistor, resistance: 1kohm }
    nets:
      A: [V1.+, R1.1]
      GND: [R1.2, V1.-, R1.1]
    """
    assert "pin_in_multiple_nets" in _rules(make_project(circuit, "tests: {}\n"))


@pytest.mark.parametrize(
    ("tests", "rule"),
    [
        (
            "tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
            "      - { measure: current(D1) }\n",
            "missing_bound",
        ),
        (
            "tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
            "      - { measure: current(D1), max: 1V }\n",
            "invalid_value",
        ),
        (
            "tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
            "      - { measure: voltage(XX), max: 1V }\n",
            "unknown_net",
        ),
        (
            "tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
            "      - { measure: current(Q9), max: 1A }\n",
            "unknown_part",
        ),
        (
            "tests:\n  t:\n    analysis: { type: tran, step: 1us, stop: 1ms }\n    expect:\n"
            "      - { measure: voltage(N1), max: 1V }\n",
            "missing_reduction",
        ),
        (
            "tests:\n  t:\n    analysis: { type: tran, step: 1ms, stop: 1us }\n    expect:\n"
            "      - { measure: max(voltage(N1)), max: 1V }\n",
            "invalid_analysis",
        ),
        (
            "tests:\n  t:\n    analysis: { type: ac }\n    expect:\n"
            "      - { measure: voltage(N1), max: 1V }\n",
            "schema",
        ),
    ],
)
def test_test_errors(make_project: MakeProject, tests: str, rule: str) -> None:
    from tests.conftest import LED_CIRCUIT

    assert rule in _rules(make_project(LED_CIRCUIT, tests))


def test_error_location(make_project: MakeProject) -> None:
    from tests.conftest import LED_CIRCUIT

    tests = (
        "tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
        "      - { measure: current(D1), max: 1V }\n"
    )
    diag = Diagnostics()
    load_design(make_project(LED_CIRCUIT, tests), diag)
    error = diag.errors[0]
    assert (error.file, error.line, error.path) == ("design/test.yaml", 5, "tests.t.expect[0].max")
