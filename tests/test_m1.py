import json
from pathlib import Path

import pytest

from eda import pipeline, vendor
from eda.design import load_design
from eda.loader import load_yaml, validate
from eda.result import Diagnostics
from eda.schema.part import PartSpec
from eda.units import QuantityError, format_quantity, parse_quantity
from tests.conftest import MakeProject, requires_ngspice

OPAMP_CIRCUIT = """
parts:
  VCC: { type: dc_voltage_source, voltage: 12V }
  VEE: { type: dc_voltage_source, voltage: 12V }
  VIN: { type: sine_voltage_source, amplitude: 0.5V, frequency: 1kHz, ac: 1V }
  U1: { type: opamp, gbw: 1MHz }
  RF: { type: resistor, resistance: 10kohm }
  RG: { type: resistor, resistance: 1kohm }
nets:
  VP: [VCC.+, U1.v+]
  VN: [VEE.-, U1.v-]
  IN: [VIN.+, U1.in+]
  FB: [U1.in-, RF.2, RG.1]
  OUT: [U1.out, RF.1]
  GND: [VCC.-, VEE.+, VIN.-, RG.2]
"""

OPAMP_TESTS = """
tests:
  bias:
    analysis: { type: op }
    expect:
      - { measure: current(U1.v+), min: 0.9mA, max: 1.1mA }
  response:
    analysis: { type: ac, start: 10Hz, stop: 10MHz }
    expect:
      - { measure: "at(db(voltage(OUT)), 1kHz)", min: 20.6dB, max: 21dB }
      - { measure: "cross(db(voltage(OUT)), 17.8dB)", min: 80kHz, max: 100kHz }
      - { measure: "at(phase(voltage(OUT)), 1kHz)", min: -2deg, max: 0deg }
  clip:
    analysis: { type: tran, step: 2us, stop: 2ms }
    expect:
      - { measure: "at(voltage(OUT), 0.25ms)", min: 5.3V, max: 5.7V }
"""


@pytest.mark.parametrize(
    ("text", "unit", "value"),
    [
        ("20dB", "dB", 20.0),
        ("-3dB", "dB", -3.0),
        ("-45deg", "deg", -45.0),
        ("0.5V/us", "V/s", 0.5e6),
        ("13V/us", "V/s", 13e6),
        ("1Tohm", "ohm", 1e12),
    ],
)
def test_new_units(text: str, unit: str, value: float) -> None:
    assert parse_quantity(text, unit) == pytest.approx(value)


def test_log_units_take_no_prefix() -> None:
    with pytest.raises(QuantityError, match="no prefix"):
        parse_quantity("1kdB", "dB")
    assert format_quantity(20.83, "dB") == "20.83dB"
    assert format_quantity(0.5e6, "V/s") == "0.5V/us"


@requires_ngspice
def test_opamp_ac_tran_op(make_project: MakeProject) -> None:
    root = make_project(OPAMP_CIRCUIT, OPAMP_TESTS, name="amp")
    result = pipeline.run(root)
    assert result.ok, result.errors
    wave = json.loads((root / "out" / "sim" / "response.json").read_text(encoding="utf-8"))
    assert wave["axis"] == "frequency"
    assert wave["signals"]["voltage(OUT)"]["imag"] is not None
    assert (root / "out" / "waves" / "response.svg").is_file()


def _rules(make_project: MakeProject, tests: str, circuit: str = OPAMP_CIRCUIT) -> list[str]:
    diag = Diagnostics()
    load_design(make_project(circuit, tests), diag)
    return [e.rule for e in diag.errors]


@pytest.mark.parametrize(
    ("measure", "rule"),
    [
        ("current(U1)", "pin_current_required"),
        ("current(RF.1)", "invalid_measure"),
        ("current(U1.vcc)", "unknown_pin"),
    ],
)
def test_pin_current_rules(make_project: MakeProject, measure: str, rule: str) -> None:
    tests = (
        "tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
        f"      - {{ measure: {measure}, max: 1A }}\n"
    )
    assert rule in _rules(make_project, tests)


def test_ac_needs_a_source(make_project: MakeProject) -> None:
    circuit = OPAMP_CIRCUIT.replace(", ac: 1V", "")
    tests = (
        "tests:\n  t:\n    analysis: { type: ac, start: 1Hz, stop: 1kHz }\n    expect:\n"
        '      - { measure: "max(db(voltage(OUT)))", max: 30dB }\n'
    )
    assert "ac_no_source" in _rules(make_project, tests, circuit)


def test_unquoted_comma_hint(make_project: MakeProject) -> None:
    tests = (
        "tests:\n  t:\n    analysis: { type: ac, start: 1Hz, stop: 1kHz }\n    expect:\n"
        "      - { measure: at(db(voltage(OUT)), 1kHz), max: 30dB }\n"
    )
    diag = Diagnostics()
    load_design(make_project(OPAMP_CIRCUIT, tests), diag)
    assert any("quote it" in e.message for e in diag.errors)


MODEL = """* Demo amplifier
* pins: non-inverting input, inverting input, supply +, supply -, output
.SUBCKT DEMOAMP 1 2 3 4
+ 5 PARAMS: GAIN=1e5
RIN 1 2 1MEG
E1 6 0 1 2 {GAIN}
R1 6 5 100
RQ 3 4 10k
.SUBCKT INNER A B
R A B 1
.ENDS
.ENDS DEMOAMP
"""


def test_inspect_model() -> None:
    (sub,) = vendor.inspect(MODEL)
    assert sub.name == "DEMOAMP"
    assert sub.nodes == ["1", "2", "3", "4", "5"]
    assert sub.params == {"GAIN": "1e5"}
    assert any("pins:" in c for c in sub.comments)


@requires_ngspice
def test_import_model(tmp_path: Path) -> None:
    source = tmp_path / "demoamp.lib"
    source.write_text(MODEL, encoding="utf-8")
    plan = vendor.plan_import(source, "demoamp", None, ["1", "2", "3", "4", "5"], None)
    assert plan.pins == ["in+", "in-", "v+", "v-", "out"]
    assert plan.compat is None  # loads in plain ngspice
    written = vendor.write_part(plan, source, None, dest=tmp_path / "vendor")
    diag = Diagnostics()
    doc = load_yaml(written, written.name, diag)
    assert doc is not None
    spec = validate(PartSpec, doc, diag)
    assert spec is not None, diag.errors
    assert spec.spice.template == "<name> <pin:in+> <pin:in-> <pin:v+> <pin:v-> <pin:out> DEMOAMP"
    assert (tmp_path / "vendor" / "models" / "demoamp.lib").is_file()


def test_import_rejects_bad_opamp_mapping(tmp_path: Path) -> None:
    source = tmp_path / "demoamp.lib"
    source.write_text(MODEL, encoding="utf-8")
    with pytest.raises(ValueError, match="permutation"):
        vendor.plan_import(source, "demoamp", None, ["1", "2", "3", "4", "9"], None)
