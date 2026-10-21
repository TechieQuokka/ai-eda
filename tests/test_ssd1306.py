"""The SSD1306 model: commands, addressing, panel mapping, and reading text back."""

import json
import shutil
from pathlib import Path

from eda import pipeline
from eda.adapters.rp2040 import I2cTransfer
from eda.design import load_design
from eda.display.font5x8 import columns
from eda.display.ssd1306 import (
    Controller,
    Ssd1306Device,
    lit_in,
    pack,
    read_text,
    unpack,
)
from eda.project import REPO_ROOT
from eda.result import Diagnostics
from tests.conftest import MakeProject
from tests.test_m4 import requires_cosim

INIT = (0xAE, 0x8D, 0x14, 0x20, 0x00, 0xA1, 0xC8, 0xDA, 0x12, 0xAF)


def _commands(*values: int) -> tuple[int, ...]:
    return (0x00, *values)  # Co = 0, D/C = 0


def _text_bytes(text: str) -> list[int]:
    data: list[int] = []
    for char in text:
        found = columns(ord(char))
        assert found is not None
        data += [*found, 0]
    return data


def _upright() -> Controller:
    lcd = Controller()
    lcd.transfer(_commands(*INIT))
    return lcd


def test_init_sequence_sets_state() -> None:
    oled = _upright()
    assert oled.on and oled.pump and oled.mode == 0 and oled.remap and oled.com_reverse


def test_single_byte_control_bytes() -> None:
    oled = Controller()
    oled.transfer((0x80, 0xAF, 0x80, 0x81, 0x80, 0x10))  # Co = 1: one command byte each
    assert oled.on and oled.contrast == 0x10  # a parameter may follow in its own control byte


def test_horizontal_addressing_wraps_within_the_window() -> None:
    oled = _upright()
    oled.transfer(_commands(0x21, 10, 11, 0x22, 2, 3))
    oled.transfer((0x40, 1, 2, 3, 4, 5))
    window = [oled.ram[p * 128 + c] for p in (2, 3) for c in (10, 11)]
    assert window == [5, 2, 3, 4]  # the fifth byte wrapped to the window's start
    assert oled.page == 2 and oled.col == 11


def test_page_addressing() -> None:
    oled = Controller()  # page addressing after reset
    oled.transfer(_commands(0xB3, 0x05, 0x12))  # page 3, column 0x25
    oled.transfer((0x40, 0xFF))
    assert oled.ram[3 * 128 + 0x25] == 0xFF


def test_orientation() -> None:
    oled = _upright()
    oled.transfer((0x40, 0x01))  # column 0, page 0, bit 0
    image = oled.pixels(128, 64, True)
    assert image[0][0]  # top left with A1 + C8
    oled.transfer(_commands(0xA0, 0xC0))
    image = oled.pixels(128, 64, True)
    assert image[63][127] and not image[0][0]  # A0 + C0: turned by 180 degrees


def test_start_line_and_invert() -> None:
    oled = _upright()
    oled.transfer((0x40, 0x02))  # RAM row 1
    oled.transfer(_commands(0x41))  # start line 1: RAM row 1 shows on the top row
    assert oled.pixels(128, 64, True)[0][0]
    oled.transfer(_commands(0xA7))
    assert not oled.pixels(128, 64, True)[0][0]


def test_read_text() -> None:
    oled = _upright()
    oled.transfer(_commands(0x21, 0, 127, 0x22, 0, 7))
    oled.transfer((0x40, *_text_bytes("Pot: 2047")))
    image = oled.pixels(128, 64, True)
    assert read_text(image) == "Pot: 2047"
    shifted = [[False] * 128 for _ in range(3)] + [[False, *row[:127]] for row in image[:61]]
    assert read_text(shifted) == "Pot: 2047"  # any pixel position, not only pages


def test_read_text_ignores_shapes() -> None:
    image = [[False] * 128 for _ in range(64)]
    for y in range(40, 52):
        for x in range(10, 100):
            image[y][x] = True
    assert read_text(image) == ""
    assert lit_in(image, (10, 40, 19, 41)) == 20


def test_pack_round_trip() -> None:
    image = [[(x * y) % 7 == 0 for x in range(128)] for y in range(64)]
    assert unpack(pack(image), 128, 64) == image


OLED = """
parts:
  V1: { type: dc_voltage_source, voltage: 3.3V }
  OLED1: { type: oled_ssd1306 }
  R1: { type: resistor, resistance: 1Mohm }
  R2: { type: resistor, resistance: 1Mohm }
nets:
  VCC: [V1.+, OLED1.vcc]
  SDA: [OLED1.sda, R1.1]
  SCL: [OLED1.scl, R2.1]
  GND: [V1.-, OLED1.gnd, R1.2, R2.2]
"""


def _device(make_project: MakeProject) -> Ssd1306Device:
    tests = "tests:\n  t:\n    analysis: { type: op }\n    expect:\n"
    tests += "      - { measure: voltage(VCC), min: 1V }\n"
    diag = Diagnostics()
    design = load_design(make_project(OLED, tests), diag)
    assert design is not None, diag.errors
    return Ssd1306Device(design.circuit.parts["OLED1"], design.circuit, 0)


def _feed(device: Ssd1306Device, *transfers: tuple[int, ...], vcc: float = 3.3) -> None:
    sent = tuple(
        I2cTransfer(1e-3 * i, 0, 0x3C, False, data, True) for i, data in enumerate(transfers)
    )
    device.window(0.0, 0.1, {}, {"VCC": vcc, "GND": 0.0}, sent)


def test_device_shows_text(make_project: MakeProject) -> None:
    device = _device(make_project)
    _feed(device, _commands(*INIT), (0x40, *_text_bytes("Hi")))
    assert device.screen(0.1).frame_at(None).text == "Hi"
    assert device.issues() == []


def test_charge_pump_off_is_dark(make_project: MakeProject) -> None:
    device = _device(make_project)
    _feed(device, _commands(0xA1, 0xC8, 0xAF), (0x40, *_text_bytes("Hi")))
    assert device.screen(0.1).frame_at(None).text == ""
    rules = dict(device.issues())
    assert "charge pump" in rules["display_dark"]


def test_wrong_com_layout_is_reported(make_project: MakeProject) -> None:
    device = _device(make_project)
    _feed(device, _commands(*INIT, 0xDA, 0x02), (0x40, *_text_bytes("Hi")))
    assert device.screen(0.1).frame_at(None).text != "Hi"  # rows interleaved
    assert "display_layout" in dict(device.issues())


def test_region_expectation_rules(make_project: MakeProject) -> None:
    lcd = "tests:\n  t:\n    analysis: { type: tran, step: 1ms, stop: 10ms }\n    expect:\n"
    cases = (
        ("{ display: OLED1, region: [0, 0, 128, 10], min: 1 }", "display_region"),
        ("{ display: OLED1, region: [0, 0, 10, 10] }", "missing_bound"),
        ("{ display: OLED1, match: x }", "display_needs_firmware"),
    )
    for i, (expect, rule) in enumerate(cases):
        diag = Diagnostics()
        load_design(make_project(OLED, lcd + f"      - {expect}\n", name=f"p{i}"), diag)
        assert rule in [e.rule for e in diag.errors], expect


EXAMPLE = REPO_ROOT / "projects" / "oled_meter"


@requires_cosim
def test_missing_pullups_kill_the_bus(tmp_path: Path) -> None:
    """A module without pull-ups on a bus without them: the controller never answers, the
    firmware reports it, and eda says why."""
    root = tmp_path / "oled_meter"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("out", "iterations.jsonl"))
    (root / "design" / "board.yaml").unlink()
    circuit = root / "design" / "circuit.yaml"
    circuit.write_text(
        circuit.read_text().replace(
            "{ type: oled_ssd1306 }", "{ type: oled_ssd1306, pullups: none }"
        )
    )
    result = pipeline.sim(root, "meter")
    warnings = {w.rule: w.message for w in result.warnings}
    assert "i2c_bus" in warnings and "pull-ups" in warnings["i2c_bus"]
    assert any(e.rule == "expect" for e in result.errors)
    assert "oled: no answer" in json.dumps(result.data["tests"])
