"""design/inputs.yaml, `keys` events, and `eda try`."""

import json
from pathlib import Path
from typing import Any

import pytest

from eda import cli
from eda.design import load_design, split_keys
from eda.model import Design
from eda.result import Diagnostics
from tests.conftest import MakeProject, requires_ngspice

# A button that lights an LED, and a pot (a key that is not a button).
CIRCUIT = """
parts:
  V1: { type: dc_voltage_source, voltage: 5V }
  SW1: { type: push_button }
  R1: { type: resistor, resistance: 330ohm }
  D1: { type: led, color: red }
  RV1: { type: potentiometer, resistance: 10kohm }
nets:
  VCC: [V1.+, SW1.1, RV1.3]
  SW: [SW1.2, R1.1]
  N1: [R1.2, D1.anode]
  WIPER: [RV1.2]
  GND: [D1.cathode, V1.-, RV1.1]
"""

INPUTS = """
keys:
  a: SW1
  turn: { part: RV1, press: { position: 90% }, release: { position: 10% } }
hold: 10ms
gap: 5ms
start: 20ms
settle: 10ms
"""


def _tests(events: str, stop: str = "100ms") -> str:
    return f"""
tests:
  t:
    analysis: {{ type: tran, step: 1ms, stop: {stop} }}
    events:
{events}
    expect:
      - {{ measure: max(current(R1)), min: 0A }}
"""


def _project(make_project: MakeProject, events: str, inputs: str | None = INPUTS) -> Path:
    circuit = CIRCUIT.replace("WIPER: [RV1.2]\n", "WIPER: [RV1.2, R2.1]\n").replace(
        "  RV1: {", "  R2: { type: resistor, resistance: 1kohm }\n  RV1: {"
    )
    circuit = circuit.replace(
        "GND: [D1.cathode, V1.-, RV1.1]", "GND: [D1.cathode, V1.-, RV1.1, R2.2]"
    )
    root = make_project(circuit, _tests(events))
    if inputs is not None:
        (root / "design" / "inputs.yaml").write_text(inputs.lstrip(), encoding="utf-8")
    return root


def _load(root: Path) -> tuple[Design | None, Diagnostics]:
    diag = Diagnostics()
    return load_design(root, diag), diag


def test_split_keys() -> None:
    assert split_keys("12+") == ["1", "2", "+"]
    assert split_keys("a{turn}a") == ["a", "turn", "a"]
    with pytest.raises(ValueError, match="no closing"):
        split_keys("{turn")


def test_keys_expand_to_presses_and_releases(make_project: MakeProject) -> None:
    root = _project(make_project, '      - { at: 20ms, keys: "a{turn}" }')
    design, diag = _load(root)
    assert design is not None, diag.errors
    events = design.tests["t"].events
    times = [round(e.time * 1000, 6) for e in events]
    assert times == [20, 30, 35, 45]  # hold 10 ms, gap 5 ms
    assert [(e.part, e.key, e.release) for e in events] == [
        ("SW1", "a", False),
        ("SW1", "a", True),
        ("RV1", "turn", False),
        ("RV1", "turn", True),
    ]
    assert events[0].values["state"][0] == "pressed"
    assert events[2].values["position"][0] == pytest.approx(90)


def test_keys_timing_override(make_project: MakeProject) -> None:
    root = _project(make_project, '      - { at: 20ms, keys: "aa", hold: 20ms, gap: 0s }')
    design, diag = _load(root)
    assert design is not None, diag.errors
    assert [round(e.time * 1000, 6) for e in design.tests["t"].events] == [20, 40, 40, 60]


@pytest.mark.parametrize(
    ("events", "inputs", "rule"),
    [
        ('      - { at: 20ms, keys: "ab" }', INPUTS, "unknown_key"),
        ('      - { at: 20ms, keys: "a" }', None, "keys_need_inputs"),
        ('      - { at: 90ms, keys: "aa" }', INPUTS, "event_time"),  # released after the end
        ('      - { at: 20ms, keys: "a" }', INPUTS.replace("a: SW1", "a: SW9"), "unknown_part"),
        ('      - { at: 20ms, keys: "a" }', INPUTS.replace("a: SW1", "a: R1"), "key_part"),
        (
            '      - { at: 20ms, keys: "a" }',
            INPUTS.replace("hold: 10ms", "hold: 0s"),
            "invalid_value",
        ),
        ('      - { at: 20ms, keys: "a" }', INPUTS.replace("90%", "190%"), "invalid_value"),
        ('      - { at: 20ms, keys: "a", part: SW1 }', INPUTS, "schema"),
    ],
)
def test_key_errors(make_project: MakeProject, events: str, inputs: str | None, rule: str) -> None:
    root = _project(make_project, events, inputs)
    design, diag = _load(root)
    assert design is None
    rules = [e.rule for e in diag.errors]
    assert rules == [rule], rules


def _try(root: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> Any:
    """`eda try` through the CLI; its JSON answer with the exit code added."""
    code = cli.main(["try", str(root), *args])
    out = json.loads(capsys.readouterr().out)
    out["exit"] = code
    return out


@requires_ngspice
def test_try_runs_keys_without_touching_tests_or_log(
    make_project: MakeProject, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(make_project, "      - { at: 20ms, part: SW1, set: { state: open } }")
    before = (root / "design" / "test.yaml").read_text(encoding="utf-8")
    out = _try(root, "--keys", "aa", "--measure", "max(current(R1))", capsys=capsys)
    assert out["ok"] is True and out["exit"] == 0, out["errors"]
    run = out["data"]["try"]
    assert run["stop"] == "60ms"  # 20 ms + 2 x (10 + 5) ms + settle 10 ms
    assert len(run["events"]) == 4
    assert run["events"][0]["event"].startswith("key a down")
    (measure,) = run["expectations"]
    assert 5e-3 < measure["value"] < 15e-3  # (5 V - LED) / 330 ohm while pressed
    assert (root / "design" / "test.yaml").read_text(encoding="utf-8") == before
    assert not (root / "iterations.jsonl").exists()
    assert (root / "out" / "try.json").is_file()


@requires_ngspice
def test_try_events_sets_and_from(
    make_project: MakeProject, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(make_project, "      - { at: 20ms, part: SW1, set: { state: pressed } }")
    out = _try(
        root,
        "--from",
        "t",
        "--event",
        "50ms SW1.state=open",
        "--set",
        "RV1.position=25%",
        "--measure",
        "at(current(R1), 40ms)",
        "--measure",
        "at(current(R1), 70ms)",
        "--measure",
        "max(voltage(WIPER))",
        capsys=capsys,
    )
    assert out["ok"] is True, out["errors"]
    run = out["data"]["try"]
    assert run["stop"] == "100ms"  # the test's own stop is longer than the inputs
    values = [e["value"] for e in run["expectations"]]
    assert values[0] > 5e-3 and abs(values[1]) < 1e-6  # pressed by the test, then opened
    assert 0.40 < values[2] < 0.47  # 25 % of the pot (2.5 k) in parallel with R2 (1 k)


@pytest.mark.parametrize(
    ("args", "rule"),
    [
        (["--event", "50ms SW1"], "invalid_option"),
        (["--event", "soon SW1.state=open"], "invalid_option"),
        (["--from", "nope"], "invalid_option"),
        (["--keys", "x"], "unknown_key"),
        (["--set", "SW1.colour=red"], "unknown_param"),
        (["--send", "10ms UART_RX hi"], "serial_needs_firmware"),
    ],
)
def test_try_errors(
    make_project: MakeProject, capsys: pytest.CaptureFixture[str], args: list[str], rule: str
) -> None:
    root = _project(make_project, "      - { at: 20ms, part: SW1, set: { state: open } }")
    out = _try(root, *args, capsys=capsys)
    assert out["ok"] is False and out["exit"] == 1
    assert [e["rule"] for e in out["errors"]] == [rule]
