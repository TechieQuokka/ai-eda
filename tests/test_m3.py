import shutil
from pathlib import Path

import pytest

from eda import pipeline
from eda.design import load_design
from eda.project import REPO_ROOT
from eda.result import Diagnostics
from tests.conftest import requires_ngspice

EXAMPLE = REPO_ROOT / "projects" / "pico_io"


@pytest.fixture
def pico(tmp_path: Path) -> Path:
    root = tmp_path / "pico_io"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("out", "iterations.jsonl"))
    (root / "design" / "board.yaml").unlink()  # boards are covered by test_m2
    return root


def _edit(root: Path, name: str, old: str, new: str) -> None:
    path = root / "design" / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _rules(root: Path) -> tuple[list[str], list[str], Diagnostics]:
    diag = Diagnostics()
    load_design(root, diag)
    return [e.rule for e in diag.errors], [w.rule for w in diag.warnings], diag


def test_example_firmware_resolves(pico: Path) -> None:
    errors, warnings, _ = _rules(pico)
    assert errors == [] and warnings == []


def test_moving_a_pin_updates_pins_h(pico: Path) -> None:
    result = pipeline.fw(pico)
    assert result.ok, result.errors
    header = (pico / "firmware" / "pins.h").read_text(encoding="utf-8")
    assert "#define PIN_LED     15u" in header

    _edit(pico, "circuit.yaml", "LED: [U1.GP15, R1.1]", "LED: [U1.GP16, R1.1]")
    result = pipeline.fw(pico)
    assert result.ok, result.errors
    header = (pico / "firmware" / "pins.h").read_text(encoding="utf-8")
    assert "#define PIN_LED     16u" in header
    assert "GP16: output" in header


def test_input_without_pull_disables_the_reset_pull_down(pico: Path) -> None:
    result = pipeline.fw(pico)
    assert result.ok, result.errors
    init = (pico / "firmware" / "pins_init.h").read_text(encoding="utf-8")
    assert "gpio_pull_up(PIN_BUTTON);" in init and "gpio_disable_pulls" not in init

    _edit(pico, "firmware.yaml", "BUTTON: { mode: input, pull: up,", "BUTTON: { mode: input,")
    result = pipeline.fw(pico)
    init = (pico / "firmware" / "pins_init.h").read_text(encoding="utf-8")
    assert "gpio_disable_pulls(PIN_BUTTON);" in init
    assert "gpio_disable_pulls(PIN_LED)" not in init  # outputs keep their reset pads


def test_wrong_function_pin_is_flagged(pico: Path) -> None:
    _edit(pico, "circuit.yaml", "UART_TX: [U1.GP0, J2.1]", "UART_TX: [U1.GP2, J2.1]")
    errors, _, diag = _rules(pico)
    assert "pin_function" in errors
    message = next(e.message for e in diag.errors if e.rule == "pin_function")
    assert "UART0_TX is not available on GP2" in message
    assert "GP0, GP12, GP16" in message


def test_function_conflict(pico: Path) -> None:
    _edit(pico, "firmware.yaml", "UART_RX: { function: UART0_RX", "UART_RX: { function: UART0_TX")
    errors, _, _ = _rules(pico)
    assert "pin_function" in errors  # GP1 cannot be UART0_TX ...
    _edit(pico, "circuit.yaml", "UART_RX: [U1.GP1, J2.2]", "UART_RX: [U1.GP12, J2.2]")
    errors, _, _ = _rules(pico)
    assert "pin_conflict" in errors  # ... and on GP12 it duplicates GP0's UART0_TX


def test_undeclared_gpio_and_unknown_signal(pico: Path) -> None:
    _edit(pico, "firmware.yaml", "  POT: { function: ADC0", "  # POT: { function: ADC0")
    _, warnings, _ = _rules(pico)
    assert "undeclared_gpio" in warnings
    _edit(pico, "test.yaml", "pins: { LED: high }", "pins: { NOPE: high }")
    errors, _, _ = _rules(pico)
    assert "unknown_signal" in errors


def test_overvoltage_on_gpio(pico: Path) -> None:
    _edit(pico, "circuit.yaml", "BUTTON: [U1.GP14, SW1.1]", "BUTTON: [U1.GP14, SW1.1, J1.+]")
    _edit(pico, "circuit.yaml", "VSYS: [J1.+, U1.VSYS]", "VSYS: [U1.VSYS, R1.2]")
    errors, _, _ = _rules(pico)
    assert "gpio_overvoltage" in errors


def test_source_scan(pico: Path) -> None:
    main = pico / "firmware" / "main.c"
    main.write_text(
        main.read_text(encoding="utf-8")
        + "\nvoid extra(void) { gpio_put(15, 1); gpio_put(PIN_BUZZER, 0); }\n",
        encoding="utf-8",
    )
    result = pipeline.fw(pico)
    assert [e.rule for e in result.errors] == ["undefined_pin"]
    assert result.errors[0].file == "firmware/main.c"
    assert [w.rule for w in result.warnings] == ["hardcoded_gpio"]
    assert "use PIN_LED" in result.warnings[0].message


@requires_ngspice
def test_firmware_drives_simulation(pico: Path) -> None:
    result = pipeline.sim(pico)
    assert result.ok, result.errors
    tests = result.data["tests"]
    assert isinstance(tests, dict)
    led_on = tests["led_on"]
    assert isinstance(led_on, dict)
    checks = led_on["expectations"]
    assert isinstance(checks, list) and isinstance(checks[0], dict)
    value = checks[0]["value"]
    assert isinstance(value, float) and 0.005 < value < 0.01
