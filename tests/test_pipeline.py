import json

import pytest

from eda import pipeline
from eda.result import ExitCode
from tests.conftest import MakeProject, requires_ngspice, requires_node

RC_CIRCUIT = """
parts:
  VIN: { type: pulse_voltage_source, high: 5V, delay: 0.1ms, width: 10ms, period: 20ms }
  R1: { type: resistor, resistance: 1kohm }
  C1: { type: capacitor, capacitance: 100nF }
nets:
  IN: [VIN.+, R1.1]
  OUT: [R1.2, C1.1]
  GND: [C1.2, VIN.-]
"""

RC_TESTS = """
tests:
  step:
    analysis: { type: tran, step: 5us, stop: 1.5ms }
    expect:
      - { measure: final(voltage(OUT)), min: 4.9V }
      - { measure: initial(voltage(OUT)), max: 0.01V }
      - { measure: max(current(R1)), min: 4.5mA, max: 5.5mA }
"""


@requires_ngspice
def test_led_op_passes_and_fails(make_project: MakeProject) -> None:
    root = make_project()
    result = pipeline.sim(root)
    # 330 ohm gives about 9.55 mA, inside 9..10 mA.
    assert result.ok, result.errors
    data = result.data["tests"]
    assert isinstance(data, dict)
    led = data["led_current"]
    assert isinstance(led, dict)
    assert led["passed"] is True
    assert (root / "out" / "sim.json").is_file()

    tighter = make_project(
        tests="tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
        "      - { measure: current(D1), min: 10mA }\n",
        name="tighter",
    )
    failed = pipeline.sim(tighter)
    assert not failed.ok
    assert failed.exit_code == ExitCode.DESIGN_ERROR
    error = failed.errors[0]
    assert error.rule == "expect"
    assert error.line == 5
    assert "too low by" in error.message


@requires_ngspice
def test_rc_transient(make_project: MakeProject) -> None:
    root = make_project(RC_CIRCUIT, RC_TESTS, name="rc")
    result = pipeline.sim(root)
    assert result.ok, result.errors
    wave = json.loads((root / "out" / "sim" / "step.json").read_text(encoding="utf-8"))
    assert "voltage(OUT)" in wave["signals"]
    assert len(wave["time"]) > 10


@requires_ngspice
@requires_node
def test_run_renders(make_project: MakeProject) -> None:
    root = make_project(RC_CIRCUIT, RC_TESTS, name="rc_render")
    result = pipeline.run(root)
    assert result.ok, result.errors
    assert (root / "out" / "schematic.svg").is_file()
    assert (root / "out" / "waves" / "step.svg").is_file()
    svg = (root / "out" / "schematic.svg").read_text(encoding="utf-8")
    assert "viewBox" in svg


@requires_ngspice
def test_simulation_failure_is_reported(make_project: MakeProject) -> None:
    # Two ideal voltage sources in parallel with different values cannot be solved.
    circuit = """
    parts:
      V1: { type: dc_voltage_source, voltage: 5V }
      V2: { type: dc_voltage_source, voltage: 3V }
      R1: { type: resistor, resistance: 1kohm }
    nets:
      A: [V1.+, V2.+, R1.1]
      GND: [V1.-, V2.-, R1.2]
    """
    tests = (
        "tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
        "      - { measure: voltage(A), max: 10V }\n"
    )
    result = pipeline.sim(make_project(circuit, tests))
    assert not result.ok
    assert result.errors[0].rule == "simulation_failed"


def test_unknown_test_name(make_project: MakeProject) -> None:
    result = pipeline.sim(make_project(), test="nope")
    assert result.errors[0].rule in {"unknown_test", "simulator_missing"}


@pytest.mark.parametrize("command", ["check", "render"])
def test_commands_write_result_files(make_project: MakeProject, command: str) -> None:
    root = make_project()
    if command == "check":
        pipeline.check(root)
    else:
        pipeline.render(root)
    assert (root / "out" / f"{command}.json").is_file()
