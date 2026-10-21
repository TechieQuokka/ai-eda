import json
from dataclasses import replace

import pytest

from eda import pipeline
from eda.adapters import ngspice_shared
from eda.design import load_design
from eda.model import Dc
from eda.netlist.spice import alter_commands
from eda.result import Diagnostics
from tests.conftest import LED_CIRCUIT, MakeProject, requires_ngspice

DIVIDER = """
parts:
  V1: { type: dc_voltage_source, voltage: 2V }
  R1: { type: resistor, resistance: 10kohm }
  R2: { type: resistor, resistance: 10kohm }
  RV1: { type: potentiometer, resistance: 10kohm }
  R3: { type: resistor, resistance: 10kohm }
nets:
  VCC: [V1.+, R1.1, RV1.1]
  OUT: [R1.2, R2.1]
  W: [RV1.2, R3.1]
  GND: [V1.-, R2.2, RV1.3, R3.2]
"""


def _tests(analysis: str, *expect: str) -> str:
    lines = "\n".join(f"      - {e}" for e in expect)
    return f"tests:\n  t:\n    analysis: {analysis}\n    expect:\n{lines}\n"


def _errors(make_project: MakeProject, analysis: str, *expect: str) -> list[str]:
    diag = Diagnostics()
    load_design(make_project(DIVIDER, _tests(analysis, *expect)), diag)
    return [e.rule for e in diag.errors]


def test_sweep_values(make_project: MakeProject) -> None:
    tests = _tests(
        "{ type: dc, sweep: V1.voltage, start: 0V, stop: 1V, step: 0.3V }",
        "{ measure: max(voltage(OUT)), max: 1V }",
    )
    diag = Diagnostics()
    design = load_design(make_project(DIVIDER, tests), diag)
    assert design is not None, diag.errors
    analysis = design.tests["t"].analysis
    assert isinstance(analysis, Dc)
    assert analysis.values == (0.0, 0.3, 0.6, 0.9, 1.0)  # always ends on stop
    assert (analysis.sweep, analysis.unit) == ("V1.voltage", "V")


@pytest.mark.parametrize(
    ("analysis", "rule"),
    [
        ("{ type: dc, sweep: V1, start: 0V, stop: 1V, step: 1mV }", "invalid_analysis"),
        ("{ type: dc, sweep: X9.voltage, start: 0V, stop: 1V, step: 1mV }", "unknown_part"),
        ("{ type: dc, sweep: V1.nope, start: 0V, stop: 1V, step: 1mV }", "unknown_param"),
        ("{ type: dc, sweep: V1.voltage, start: 1V, stop: 0V, step: 1mV }", "invalid_analysis"),
        ("{ type: dc, sweep: V1.voltage, start: 0V, stop: 1V, step: 0V }", "invalid_analysis"),
        ("{ type: dc, sweep: V1.voltage, start: 0A, stop: 1V, step: 1mV }", "invalid_value"),
        (
            "{ type: dc, sweep: R1.resistance, start: 0ohm, stop: 1kohm, step: 1ohm }",
            "invalid_value",
        ),
        ("{ type: dc, sweep: V1.voltage, start: 0V, stop: 100V, step: 1mV }", "too_many_points"),
    ],
)
def test_sweep_errors(make_project: MakeProject, analysis: str, rule: str) -> None:
    assert _errors(make_project, analysis, "{ measure: max(voltage(OUT)), max: 1V }") == [rule]


def test_sweep_needs_an_alterable_parameter(make_project: MakeProject) -> None:
    circuit = """
    parts:
      V1: { type: sine_voltage_source, amplitude: 1V, frequency: 1kHz }
      R1: { type: resistor, resistance: 1kohm }
    nets:
      A: [V1.+, R1.1]
      GND: [V1.-, R1.2]
    """
    tests = _tests(
        "{ type: dc, sweep: V1.amplitude, start: 0V, stop: 1V, step: 0.1V }",
        "{ measure: max(voltage(A)), max: 1V }",
    )
    diag = Diagnostics()
    load_design(make_project(circuit, tests), diag)
    assert [e.rule for e in diag.errors] == ["sweep_unsupported"]


@pytest.mark.parametrize(
    ("measure", "rule"),
    [
        ("voltage(OUT)", "missing_reduction"),
        ("avg(voltage(OUT))", "invalid_measure"),
        ('"at(voltage(OUT), 1ms)"', "invalid_measure"),  # at() takes a swept value (V)
        ('"period(voltage(OUT), 1V)"', "invalid_measure"),
    ],
)
def test_sweep_measure_rules(make_project: MakeProject, measure: str, rule: str) -> None:
    analysis = "{ type: dc, sweep: V1.voltage, start: 0V, stop: 1V, step: 0.1V }"
    assert _errors(make_project, analysis, f"{{ measure: {measure}, max: 1V }}") == [rule]


def test_source_dc_value_can_change() -> None:
    from eda.netlist import spice

    assert spice._source_dc_change("V1 a 0 DC 2 AC 0", "V1 a 0 DC 3.5 AC 0") == (  # pyright: ignore[reportPrivateUsage]
        "alter V1 dc = 3.5"
    )
    assert spice._source_dc_change("V1 a 0 DC 2 AC 0", "V1 a 0 DC 2 AC 1") is None  # pyright: ignore[reportPrivateUsage]
    assert spice._source_dc_change("R1 a 0 1k", "R1 a 0 2k") is None  # pyright: ignore[reportPrivateUsage]


def test_pot_position_alters_both_halves(make_project: MakeProject) -> None:
    diag = Diagnostics()
    design = load_design(make_project(DIVIDER, "tests: {}\n"), diag)
    assert design is not None
    old = design.circuit.parts["RV1"]
    new = replace(old, values={**old.values, "position": 25.0})
    assert len(alter_commands(old, new, design.circuit)) == 2


@requires_ngspice
def test_sweep_simulates(make_project: MakeProject) -> None:
    # LED_CIRCUIT: 330 Ω in series with a red LED; sweep the supply.
    tests = _tests(
        "{ type: dc, sweep: V1.voltage, start: 0V, stop: 5V, step: 50mV }",
        '{ measure: "cross(current(D1), 1mA)", min: 1.5V, max: 2.2V }',
        '{ measure: "at(current(D1), 5V)", min: 9mA, max: 10mA }',
        "{ measure: initial(current(D1)), max: 1uA }",
        "{ measure: pp(voltage(N1)), min: 1V }",
    )
    root = make_project(LED_CIRCUIT, tests)
    result = pipeline.sim(root)
    assert result.ok, result.errors
    data = json.loads((root / "out" / "sim" / "t.json").read_text(encoding="utf-8"))
    assert (data["axis"], data["x_label"], data["x_unit"]) == ("sweep", "V1.voltage", "V")
    assert len(data["x"]) == 101 and data["x"][-1] == 5.0
    # the point at 5 V is the plain operating point
    op = pipeline.sim(make_project(LED_CIRCUIT, name="op"))
    tests_data = op.data["tests"]
    assert isinstance(tests_data, dict)
    point = tests_data["led_current"]
    assert isinstance(point, dict)
    ops = point["operating_point"]
    assert isinstance(ops, dict)
    current = ops["current(D1)"]
    assert isinstance(current, dict)
    assert data["signals"]["current(D1)"]["values"][-1] == pytest.approx(current["value"])


@requires_ngspice
def test_sweep_plot_marks_the_crossing(make_project: MakeProject) -> None:
    tests = _tests(
        "{ type: dc, sweep: V1.voltage, start: 0V, stop: 5V, step: 0.1V }",
        '{ measure: "cross(current(D1), 1mA)", min: 1.5V, max: 2.2V }',
    )
    root = make_project(LED_CIRCUIT, tests)
    assert pipeline.sim(root).ok
    result = pipeline.render(root)
    assert result.ok, result.errors
    drawn = (root / "out" / "waves" / "t.svg").read_text(encoding="utf-8")
    assert "sweep V1.voltage" in drawn


@pytest.mark.skipif(ngspice_shared.library_path() is None, reason="needs libngspice")
def test_supply_step_event(make_project: MakeProject) -> None:
    tests = _tests(
        "{ type: tran, step: 100us, stop: 20ms }",
        '{ measure: "at(voltage(VCC), 5ms)", min: 4.99V, max: 5.01V }',
        '{ measure: "at(voltage(VCC), 15ms)", min: 2.99V, max: 3.01V }',
    ).replace(
        "    expect:", "    events: [{ at: 10ms, part: V1, set: { voltage: 3V } }]\n    expect:"
    )
    result = pipeline.sim(make_project(LED_CIRCUIT, tests))
    assert result.ok, result.errors
