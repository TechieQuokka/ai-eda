import json

import pytest

from eda import pipeline
from eda.design import load_design
from eda.display.segments import CHARACTERS, character
from eda.model import Op
from eda.netlist.spice import build_netlist
from eda.result import Diagnostics
from tests.conftest import MakeProject, requires_ngspice

# Digit 1 shows segments b and c ("1") from a supply; a 100 kohm keeps the other pins
# defined. The pulse source drives digit 2's "1" for 2 ms in every 10 ms (one slot of a
# five-digit multiplex), and its decimal point stays open (an optional pin).
SEGMENTS = """
parts:
  V1: { type: dc_voltage_source, voltage: 3.3V }
  V2: { type: pulse_voltage_source, low: 3.3V, high: 0V, delay: 0s, rise: 1us, fall: 1us,
        width: 2ms, period: 10ms }
  RB: { type: resistor, resistance: 100ohm }
  RC: { type: resistor, resistance: 100ohm }
  RX: { type: resistor, resistance: 100kohm }
  DS1: { type: seven_segment_4 }
nets:
  VCC: [V1.+, RB.1, RC.1]
  SB: [RB.2, DS1.b]
  SC: [RC.2, DS1.c]
  OFF: [DS1.a, DS1.d, DS1.e, DS1.f, DS1.g, RX.1]
  K2: [V2.+, DS1.d2]
  HIGH: [DS1.d3, DS1.d4, RX.2]
  GND: [V1.-, V2.-, DS1.d1]
"""


def _tests(*expect: str, analysis: str = "{ type: op }") -> str:
    lines = "\n".join(f"      - {e}" for e in expect)
    return f"tests:\n  t:\n    analysis: {analysis}\n    expect:\n{lines}\n"


def _rules(make_project: MakeProject, tests: str) -> list[str]:
    diag = Diagnostics()
    load_design(make_project(SEGMENTS, tests), diag)
    return [e.rule for e in diag.errors]


def test_characters() -> None:
    assert character(["b", "c"]) == "1"
    assert character(["a", "b", "c", "d", "e", "f", "g", "dp"]) == "8"  # dp is not a shape
    assert character(["g", "a"]) == "?"
    assert character([]) == " "
    assert set("0123456789AbCdEF-") <= set(CHARACTERS)
    assert all(character(shape) == char for char, shape in CHARACTERS.items())


def test_netlist_has_one_measured_led_per_digit_and_segment(make_project: MakeProject) -> None:
    diag = Diagnostics()
    design = load_design(make_project(SEGMENTS, _tests("{ display: DS1, match: '1' }")), diag)
    assert design is not None, diag.errors
    netlist = build_netlist("t", design.circuit, Op(), design.paths.out_dir / "x.raw")
    leds = [line for line in netlist.splitlines() if line.startswith("DS1_")]
    assert len(leds) == 4 * 7  # dp is not connected: no LEDs for it
    assert "VDS1_1b s_ds1_1 DS1_1bn DC 0" in netlist  # pin b is sensed (pin_currents)


@pytest.mark.parametrize(
    ("expect", "analysis", "rule"),
    [
        ("{ display: RX, match: '1' }", "{ type: op }", "display_part"),
        ("{ display: NOPE, match: '1' }", "{ type: op }", "display_part"),
        ("{ display: DS1, match: '(' }", "{ type: op }", "invalid_match"),
        ("{ display: DS1, match: '(1)(2)' }", "{ type: op }", "invalid_match"),
        ("{ display: DS1, match: '1', at: 1ms }", "{ type: op }", "display_analysis"),
        (
            "{ display: DS1, match: '1', at: 2s }",
            "{ type: tran, step: 1ms, stop: 1s }",
            "display_time",
        ),
        (
            "{ display: DS1, match: '1' }",
            "{ type: ac, start: 1Hz, stop: 1kHz }",
            "display_analysis",
        ),
    ],
)
def test_display_expectation_rules(
    make_project: MakeProject, expect: str, analysis: str, rule: str
) -> None:
    assert rule in _rules(make_project, _tests(expect, analysis=analysis))


@pytest.mark.parametrize(
    "expect",
    [
        "{ display: DS1, match: '1', at: 1ms }",  # at without a tran test is caught by pydantic
        "{ measure: current(RB), match: '1', min: 1mA }",
        "{ display: DS1 }",
    ],
)
def test_display_expectation_shape(make_project: MakeProject, expect: str) -> None:
    rules = _rules(make_project, _tests(expect, analysis="{ type: op }"))
    assert rules, expect


@requires_ngspice
def test_operating_point_reads_the_lit_segments(make_project: MakeProject) -> None:
    tests = _tests(
        "{ display: DS1, match: '^1   $' }",
        "{ display: DS1, match: '(\\d)', min: 1, max: 1 }",
        "{ display: DS1, match: '1', min: 1, max: 1 }",  # count of matches
    )
    root = make_project(SEGMENTS, tests)  # in the operating point V2 holds digit 2 off
    result = pipeline.sim(root)
    assert result.ok, result.errors
    test = result.data["tests"]
    assert isinstance(test, dict)
    shown = test["t"]["displays"]["DS1"]  # type: ignore[index]
    assert shown["text"] == "1   "  # type: ignore[index]
    assert (root / "out" / "display" / "t.DS1.svg").is_file()
    frames = json.loads((root / "out" / "display" / "t.DS1.json").read_text())
    assert frames["kind"] == "segments" and len(frames["frames"]) == 1


@requires_ngspice
def test_dim_digit_reads_as_dark(make_project: MakeProject) -> None:
    """Digit 2 is on for 2 ms of every 10 ms: well above the 100 uA visibility floor on
    average, but under 30 % of the always-on digit 1, so it reads as dark (ghosting)."""
    tests = _tests(
        "{ display: DS1, match: '^1   $', at: 50ms }",
        analysis="{ type: tran, step: 20us, stop: 60ms }",
    )
    root = make_project(SEGMENTS, tests)
    result = pipeline.sim(root)
    assert result.ok, result.errors
    frames = json.loads((root / "out" / "display" / "t.DS1.json").read_text())
    digits = frames["frames"][-1]["state"]["digits"]
    assert 1e-3 < digits[1]["mean"]["b"] < 0.3 * digits[0]["mean"]["b"]
    assert digits[0]["lit"] == ["b", "c"] and digits[1]["lit"] == []
