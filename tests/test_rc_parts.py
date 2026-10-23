"""Parts added for the RC car and its remote: DC motor, DRV8833 carrier, S7V8F5 regulator,
servo, HC-05, joystick, the 1N5822 Schottky, and repeated serial events."""

import shutil
from pathlib import Path

import pytest

from eda import pipeline
from eda.adapters import ngspice_shared
from eda.design import load_design
from eda.project import REPO_ROOT
from eda.result import Diagnostics
from tests.conftest import MakeProject, requires_ngspice

pytestmark = requires_ngspice


def _passes(make_project: MakeProject, circuit: str, tests: str) -> None:
    result = pipeline.sim(make_project(circuit, tests))
    assert result.ok, result.errors


MOTOR = """
parts:
  V1: { type: dc_voltage_source, voltage: 6V }
  M1: { type: dc_motor }
nets:
  VM: [V1.+, M1.+]
  GND: [V1.-, M1.-]
"""


def test_motor_currents(make_project: MakeProject) -> None:
    # Defaults: 130-size motor at 6 V, 2.2 A stall, 0.2 A no load, 20 ms time constant.
    # Spin-up: about I0 + (Istall - I0) e^(-t / tm), so 0.94 A after 20 ms.
    _passes(
        make_project,
        MOTOR,
        """
        tests:
          running:
            analysis: { type: op }
            expect:
              - { measure: current(M1), min: 0.199A, max: 0.201A }
          stalled:
            analysis: { type: op }
            parts: { M1: { shaft: stalled } }
            expect:
              - { measure: current(M1), min: 2.19A, max: 2.21A }
          spin_up:
            analysis: { type: tran, step: 10us, stop: 150ms }
            parts: { V1: { voltage: 0V } }
            events:
              - { at: 10ms, part: V1, set: { voltage: 6V } }
              - { at: 100ms, part: M1, set: { shaft: stalled } }
            expect:
              - { measure: "at(current(M1), 30ms)", min: 0.85A, max: 1.0A }
              - { measure: max(current(M1)), to: 99ms, min: 2.1A, max: 2.2A }
              - { measure: "at(current(M1), 99ms)", min: 0.2A, max: 0.23A }
              - { measure: final(current(M1)), min: 2.19A, max: 2.21A }
        """,
    )


DRIVER = """
parts:
  V1: { type: dc_voltage_source, voltage: 6V }
  VA: { type: dc_voltage_source, voltage: 3.3V }
  VB: { type: dc_voltage_source, voltage: 0V }
  U1: { type: drv8833_carrier }
  RL: { type: resistor, resistance: 10ohm }
nets:
  VIN: [V1.+, U1.vin]
  IN1: [VA.+, U1.ain1]
  IN2: [VB.+, U1.ain2]
  OUT1: [U1.aout1, RL.1]
  OUT2: [U1.aout2, RL.2]
  GND: [V1.-, VA.-, VB.-, U1.gnd]
"""


@pytest.mark.parametrize(
    ("in1", "in2", "amps"),
    [
        ("3.3V", "0V", 6 / 10.39),  # forward: 200 + 160 mohm bridge, 30 mohm protection
        ("0V", "3.3V", -6 / 10.39),  # reverse
        ("3.3V", "3.3V", 0.0),  # brake: both outputs low
        ("0V", "0V", 0.0),  # coast: both open
        ("3.3V", "1.65V", 0.5 * 6 / 10.39),  # slow decay at 50 % (averaged PWM)
    ],
)
def test_drv8833_logic(make_project: MakeProject, in1: str, in2: str, amps: float) -> None:
    low, high = sorted((amps - 0.01, amps + 0.01))
    _passes(
        make_project,
        DRIVER,
        f"""
        tests:
          t:
            analysis: {{ type: op }}
            parts: {{ VA: {{ voltage: {in1} }}, VB: {{ voltage: {in2} }} }}
            expect:
              - {{ measure: current(RL), min: {low}A, max: {high}A }}
        """,
    )


def test_drv8833_protection(make_project: MakeProject) -> None:
    # Below the undervoltage lockout and with the supply reversed, nothing is driven.
    _passes(
        make_project,
        DRIVER,
        """
        tests:
          uvlo:
            analysis: { type: op }
            parts: { V1: { voltage: 2.4V } }
            expect:
              - { measure: current(RL), min: -1mA, max: 1mA }
          reversed:
            analysis: { type: op }
            parts: { V1: { voltage: -6V } }
            expect:
              - { measure: current(RL), min: -1mA, max: 1mA }
              - { measure: current(U1.vin), min: -1mA, max: 1mA }
        """,
    )


REGULATOR = """
parts:
  V1: { type: dc_voltage_source, voltage: 6V }
  U1: { type: pololu_s7v8f5 }
  RL: { type: resistor, resistance: 10ohm }
nets:
  VIN: [V1.+, U1.vin]
  OUT: [U1.vout, RL.1]
  GND: [V1.-, U1.gnd, RL.2]
"""


def test_s7v8f5(make_project: MakeProject) -> None:
    # 500 mA out: 5 V from 6 V and from 3.5 V; 5 ohm at 3 V asks 1 A, but the module
    # gives 0.34 A/V x (3 V - 0.74 V) = 0.77 A, so the output falls to 3.84 V.
    _passes(
        make_project,
        REGULATOR,
        """
        tests:
          step_down:
            analysis: { type: op }
            expect:
              - { measure: voltage(OUT), min: 4.99V, max: 5.0V }
              - { measure: current(U1.vin), min: 0.47A, max: 0.48A }
          step_up:
            analysis: { type: op }
            parts: { V1: { voltage: 3.5V } }
            expect:
              - { measure: voltage(OUT), min: 4.99V, max: 5.0V }
          limited:
            analysis: { type: op }
            parts: { V1: { voltage: 3V }, RL: { resistance: 5ohm } }
            expect:
              - { measure: voltage(OUT), min: 3.82V, max: 3.86V }
          off:
            analysis: { type: op }
            parts: { V1: { voltage: 2.4V } }
            expect:
              - { measure: voltage(OUT), max: 10mV }
        """,
    )


MODULES = """
parts:
  V5: { type: dc_voltage_source, voltage: 5V }
  V3: { type: dc_voltage_source, voltage: 3.3V }
  S1: { type: servo_sg90 }
  U1: { type: hc05 }
  RS: { type: resistor, resistance: 100kohm }
  J1: { type: joystick_2axis, x: 100%, y: 25% }
  RP: { type: resistor, resistance: 50kohm }
  RX: { type: resistor, resistance: 10Mohm }
  RY: { type: resistor, resistance: 10Mohm }
nets:
  V5: [V5.+, S1.vplus, U1.vcc]
  V3: [V3.+, J1.vcc, RP.1, S1.signal]
  STATE: [U1.state, RS.1]
  X: [J1.vrx, RX.1]
  Y: [J1.vry, RY.1]
  SW: [J1.sw, RP.2]
  GND: [V5.-, V3.-, S1.gnd, U1.gnd, RS.2, J1.gnd, RX.2, RY.2]
"""


def test_modules(make_project: MakeProject) -> None:
    _passes(
        make_project,
        MODULES,
        """
        tests:
          idle:
            analysis: { type: op }
            expect:
              - { measure: current(S1.vplus), min: 5.9mA, max: 6.1mA }
              - { measure: current(U1.vcc), min: 29.9mA, max: 30.1mA }
              - { measure: voltage(STATE), min: 3.29V, max: 3.3V }
              - { measure: voltage(X), min: 3.299V, max: 3.3V }
              - { measure: voltage(Y), min: 0.82V, max: 0.83V }
              - { measure: voltage(SW), min: 3.29V }
          busy:
            analysis: { type: op }
            parts:
              S1: { load: stalled }
              U1: { link: waiting }
              J1: { button: pressed }
            expect:
              - { measure: current(S1.vplus), min: 640mA, max: 660mA }
              - { measure: voltage(STATE), max: 1mV }
              - { measure: voltage(SW), max: 1mV }
        """,
    )


SCHOTTKY = """
parts:
  I1: { type: dc_voltage_source, voltage: 0V }
  RL: { type: resistor, resistance: 1ohm }
  D1: { type: diode, model: 1N5822 }
nets:
  A: [I1.+, D1.anode]
  K: [D1.cathode, RL.1]
  GND: [I1.-, RL.2]
"""


def test_1n5822(make_project: MakeProject) -> None:
    # Within the datasheet maxima: 0.525 V at 3 A (about 3.5 V across diode and 1 ohm).
    _passes(
        make_project,
        SCHOTTKY,
        """
        tests:
          one_amp:
            analysis: { type: op }
            parts: { I1: { voltage: 1.37V } }
            expect:
              - { measure: "voltage(A, K)", min: 0.35V, max: 0.39V }
          three_amps:
            analysis: { type: op }
            parts: { I1: { voltage: 3.5V } }
            expect:
              - { measure: "voltage(A, K)", min: 0.45V, max: 0.525V }
        """,
    )


@pytest.fixture
def pico(tmp_path: Path) -> Path:
    root = tmp_path / "pico_io"
    example = REPO_ROOT / "projects" / "pico_io"
    shutil.copytree(example, root, ignore=shutil.ignore_patterns("out", "iterations.jsonl"))
    (root / "design" / "board.yaml").unlink()
    return root


def _add_test(pico: Path, text: str) -> None:
    tests = pico / "design" / "test.yaml"
    tests.write_text(tests.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_repeated_send(pico: Path) -> None:
    _add_test(
        pico,
        """  keep_alive:
    analysis: { type: tran, step: 1ms, stop: 100ms }
    firmware: true
    events:
      - { at: 10ms, serial: UART_RX, send: "rate 100\\n", every: 20ms }
      - { at: 5ms, serial: UART_RX, send: "x\\n", every: 30ms, until: 65ms }
    expect:
      - { serial: UART_TX, match: "rate=100", min: 1 }
""",
    )
    diag = Diagnostics()
    design = load_design(pico, diag)
    assert design is not None and not diag.errors, diag.errors
    events = design.tests["keep_alive"].events
    times = [round(e.time * 1000, 6) for e in events]
    assert times == [5, 10, 30, 35, 50, 70, 90]  # `until` is exclusive
    heads = [e for e in events if not e.repeat]
    assert [(e.text, e.repeats) for e in heads] == [("x\n", 1), ("rate 100\n", 4)]


def test_repeated_send_rules(pico: Path) -> None:
    _add_test(
        pico,
        """  bad_repeat:
    analysis: { type: tran, step: 1ms, stop: 100ms }
    firmware: true
    events:
      - { at: 10ms, serial: UART_RX, send: "x", every: 0s }
      - { at: 10ms, serial: UART_RX, send: "x", every: 1ms, until: 200ms }
      - { at: 10ms, serial: UART_RX, send: "x", every: 100ns }
    expect:
      - { serial: UART_TX, match: "x", min: 1 }
""",
    )
    diag = Diagnostics()
    load_design(pico, diag)
    assert sorted(e.rule for e in diag.errors) == ["event_time", "invalid_value", "invalid_value"]


def test_repeat_needs_serial(make_project: MakeProject) -> None:
    root = make_project(
        tests="""
        tests:
          t:
            analysis: { type: tran, step: 1ms, stop: 10ms }
            events:
              - { at: 1ms, part: D1, set: { color: red }, every: 1ms }
            expect:
              - { measure: max(current(D1)), max: 1A }
        """
    )
    diag = Diagnostics()
    load_design(root, diag)
    assert [(e.rule, "go with `serial`" in e.message) for e in diag.errors] == [("schema", True)]


def test_small_steps_resume() -> None:
    # A 50 us co-simulation step makes ngspice's maximum step 10 us. With a tolerance in
    # the halt condition the run stopped short of each window's end (here by 1e-9 of
    # it); in the RC car that gap fell below ngspice's minimum step, it could not resume
    # ("timestep too small"), and the stepped runner looped forever.
    boost = REPO_ROOT / "parts" / "models" / "eda_boost.lib"
    netlist = "\n".join(
        [
            "* resume",
            f'.include "{boost}"',
            ".model dsch D(IS=48.5u RS=0.061 N=1.2 CJO=300p M=0.4 VJ=0.6 BV=40 IBV=2m)",
            "V1 bat 0 DC 6",
            "R1 bat a 0.8",
            "D1 a vreg dsch",
            "X1 vreg 0 v5 shdn eda_boost vset=5 eta=88 iq=0.2m von=2.7 voff=2.5 rout=0.01",
            "RL v5 0 833",
            "C1 a 0 470u",
            ".end",
        ]
    )
    with ngspice_shared.session(netlist) as sim:
        times = [sim.start("tran 5e-05 0.003 0 1e-05", 5e-7)]
        times += [sim.run_until(5e-7 + k * 5e-5) for k in range(1, 5)]
    assert times == pytest.approx([5e-7 + k * 5e-5 for k in range(5)], rel=1e-12, abs=0)


def test_halts_land_on_window_ends() -> None:
    # ngspice lands a few ULPs short of a breakpoint; with the breakpoint at the window's
    # end and an exact halt condition, 42 of these 150 halts missed it and ran on for a
    # whole maximum step (20 us), which put the emulator's pin timing out of step with
    # the circuit (the calculator's LCD saw 0 ns enable pulses).
    netlist = "\n".join(["* windows", "V1 a 0 DC 1", "R1 a b 1k", "C1 b 0 1u", ".end"])
    start = 1.10472e-4
    with ngspice_shared.session(netlist) as sim:
        times = [sim.start("tran 0.001 0.2 0 0.0002", start)]
        times += [sim.run_until(start + k * 1e-3) for k in range(1, 150)]
    assert times == pytest.approx([start + k * 1e-3 for k in range(150)], rel=1e-12, abs=0)
