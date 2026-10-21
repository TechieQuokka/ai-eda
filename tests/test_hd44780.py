"""The HD44780 model, driven with synthetic bus timing (no emulator needed)."""

from dataclasses import dataclass, field
from xml.etree import ElementTree

import pytest

from eda.design import load_design
from eda.display.font5x8 import character, code_of, columns, glyph
from eda.display.hd44780 import Controller, Hd44780Device, draw
from eda.result import Diagnostics
from tests.conftest import MakeProject

# The LCD's bus nets end in resistors; the tests stand in for the firmware's GPIOs.
LCD = """
parts:
  V1: { type: dc_voltage_source, voltage: 5V }
  LCD1: { type: lcd_1602 }
  R1: { type: resistor, resistance: 1kohm }
  R2: { type: resistor, resistance: 1kohm }
  R3: { type: resistor, resistance: 1kohm }
  R4: { type: resistor, resistance: 1kohm }
  R5: { type: resistor, resistance: 1kohm }
  R6: { type: resistor, resistance: 1kohm }
  R7: { type: resistor, resistance: 1kohm }
nets:
  VDD: [V1.+, LCD1.vdd]
  V0: [LCD1.v0, R7.1]
  RS: [LCD1.rs, R1.1]
  E: [LCD1.e, R2.1]
  D4: [LCD1.d4, R3.1]
  D5: [LCD1.d5, R4.1]
  D6: [LCD1.d6, R5.1]
  D7: [LCD1.d7, R6.1]
  GND: [V1.-, LCD1.vss, LCD1.rw, R1.2, R2.2, R3.2, R4.2, R5.2, R6.2, R7.2]
"""
BUS = ("RS", "E", "D4", "D5", "D6", "D7")
SUPPLY = {"VDD": 5.0, "V0": 0.6, "GND": 0.0}


@dataclass
class Bus:
    """Firmware stand-in: GPIO levels changed at given times, handed over in windows."""

    device: Hd44780Device
    supply: dict[str, float] = field(default_factory=lambda: dict(SUPPLY))
    vio: float = 3.3
    t: float = 0.0
    window_start: float = 0.0
    state: dict[str, str] = field(default_factory=lambda: dict.fromkeys(BUS, "low"))
    changes: dict[str, list[tuple[float, str]]] = field(
        default_factory=lambda: {n: [] for n in BUS}
    )

    def put(self, net: str, high: bool | int, after: float = 100e-9) -> None:
        self.t += after
        state = "high" if high else "low"
        if self.state[net] != state:
            self.state[net] = state
            self.changes[net].append((self.t, state))

    def wait(self, seconds: float) -> None:
        self.t += seconds

    def flush(self) -> None:
        """Hand everything up to now to the device as one window."""
        gpio: dict[str, list[tuple[float, str]]] = {}
        for net in BUS:
            changes = self.changes[net]
            first = changes[0][1] if changes else self.state[net]
            start_state = self._state_before(net, first)
            gpio[net] = [(self.window_start, start_state), *changes]
            self.changes[net] = []
        volts = {
            **self.supply,
            **{n: self.vio if s == "high" else 0.0 for n, s in self.state.items()},
        }
        self.device.window(self.window_start, self.t, gpio, volts)
        self.window_start = self.t

    def _state_before(self, net: str, first_change: str) -> str:
        if not self.changes[net]:
            return self.state[net]
        return "low" if first_change == "high" else "high"

    # HD44780 4-bit protocol, with timing that meets the datasheet unless told otherwise

    def nibble(self, value: int, e_high: float = 1e-6) -> None:
        for bit, net in enumerate(("D4", "D5", "D6", "D7")):
            self.put(net, (value >> bit) & 1)
        self.put("E", 1, after=200e-9)
        self.put("E", 0, after=e_high)
        self.wait(1e-6)

    def byte(self, data: bool, value: int, wait: float | None = None) -> None:
        self.put("RS", data)
        self.nibble(value >> 4)
        self.nibble(value & 0x0F)
        self.wait(wait if wait is not None else (2e-3 if not data and value <= 3 else 50e-6))

    def init(self) -> None:
        self.wait(50e-3)
        for value, pause in ((0x3, 5e-3), (0x3, 150e-6), (0x3, 150e-6), (0x2, 150e-6)):
            self.nibble(value)
            self.wait(pause)
        for value in (0x28, 0x0C, 0x01, 0x06):
            self.byte(False, value)

    def text(self, text: str) -> None:
        for char in text:
            self.byte(True, ord(char))


def _tests(expect: str) -> str:
    return f"tests:\n  t:\n    analysis: {{ type: op }}\n    expect:\n      - {expect}\n"


def _device(make_project: MakeProject, vio: float = 3.3) -> Hd44780Device:
    tests = _tests("{ measure: voltage(VDD), min: 1V }")
    diag = Diagnostics()
    design = load_design(make_project(LCD, tests), diag)
    assert design is not None, diag.errors
    return Hd44780Device(design.circuit.parts["LCD1"], design.circuit, vio, set(BUS))


def _text(device: Hd44780Device, end: float) -> str:
    return device.screen(end).frame_at(None).text


def _rules(device: Hd44780Device) -> list[str]:
    return [rule for rule, _ in device.issues()]


@pytest.fixture
def bus(make_project: MakeProject) -> Bus:
    bus = Bus(_device(make_project))
    bus.flush()  # power-up
    return bus


def test_init_and_text(bus: Bus) -> None:
    bus.init()
    bus.text("Hello")
    bus.byte(False, 0xC0)  # line 2
    bus.text("world")
    bus.flush()
    assert _text(bus.device, bus.t) == "Hello\nworld"
    assert _rules(bus.device) == []


def test_display_off_until_initialised(bus: Bus) -> None:
    bus.wait(30e-3)
    bus.flush()
    assert _text(bus.device, bus.t) == "\n"


def test_write_while_busy_is_ignored(bus: Bus) -> None:
    bus.init()
    bus.byte(False, 0x01, wait=100e-6)  # clear needs 1.52 ms
    bus.text("A")
    bus.wait(2e-3)
    bus.text("B")
    bus.flush()
    assert _text(bus.device, bus.t) == "B\n"  # "A" was lost
    rules = dict(bus.device.issues())
    assert "display_busy" in rules and "1.52ms" in rules["display_busy"]


def test_writes_during_power_on_reset_are_ignored(bus: Bus) -> None:
    for value in (0x3, 0x3, 0x3, 0x2):  # no 50 ms wait first
        bus.nibble(value)
    bus.flush()
    assert "display_busy" in _rules(bus.device)


def test_short_enable_pulse_is_reported(bus: Bus) -> None:
    bus.init()
    bus.put("RS", 1)
    bus.nibble(0x4, e_high=100e-9)
    bus.nibble(0x1)
    bus.flush()
    issues = dict(bus.device.issues())
    assert "E high for 100 ns (needs 230 ns)" in issues["display_timing"]


def test_missing_four_bit_switch_garbles(bus: Bus) -> None:
    bus.wait(50e-3)
    for value in (0x28, 0x0C, 0x01, 0x06):  # still in 8-bit mode: each nibble is a byte
        bus.byte(False, value)
    bus.text("Hi")
    bus.flush()
    assert _text(bus.device, bus.t) != "Hi\n"


def test_low_contrast_reads_blank(bus: Bus) -> None:
    bus.supply["V0"] = 2.5  # VDD-V0 = 2.5 V: below 3.0 V nothing is visible
    bus.init()
    bus.text("Hi")
    bus.flush()
    assert _text(bus.device, bus.t) == "\n"
    assert "display_contrast" in _rules(bus.device)


def test_low_supply_is_reported(make_project: MakeProject) -> None:
    bus = Bus(_device(make_project), supply={"VDD": 3.3, "V0": 0.0, "GND": 0.0})
    bus.flush()
    bus.init()
    bus.text("Hi")
    bus.flush()
    assert _text(bus.device, bus.t) == "\n"
    assert "display_unpowered" in _rules(bus.device)


def test_weak_high_level_is_undefined(make_project: MakeProject) -> None:
    bus = Bus(_device(make_project, vio=1.8), vio=1.8)  # 1.8 V logic, 2.2 V threshold
    bus.flush()
    bus.init()
    bus.flush()
    assert "display_logic_level" in _rules(bus.device)


def test_controller_addressing() -> None:
    lcd = Controller(columns=16, rows=2)
    lcd.write(0, False, 0x38)  # 8-bit, two lines
    lcd.write(1, False, 0x0C)
    lcd.write(2, False, 0x80 | 0x27)  # last position of line 1
    lcd.write(3, True, ord("A"))
    lcd.write(4, True, ord("B"))  # continues at the start of line 2
    assert lcd.ddram[0x27] == ord("A") and lcd.ddram[0x40] == ord("B")
    lcd.write(5, False, 0x18)  # shift the display left by one
    assert lcd.codes()[1][15] == 0x20 and lcd.cell(1, 0) == 0x41
    lcd.write(6, False, 0x30)  # one-line mode: only the first row shows
    assert lcd.cell(1, 0) is None


def test_cgram_characters() -> None:
    lcd = Controller(columns=16, rows=2)
    lcd.write(0, False, 0x38)
    lcd.write(1, False, 0x40 | 8)  # CGRAM character 1
    for t, row in enumerate((0x0E, 0x11, 0x11, 0x0E, 0, 0, 0, 0)):
        lcd.write(2 + t, True, row)
    lcd.write(20, False, 0x80)
    lcd.write(21, True, 0x01)
    assert lcd.cgram[8:12] == [0x0E, 0x11, 0x11, 0x0E]
    assert character(lcd.codes()[0][0]) == "\x01"


def test_cgram_characters_are_drawn(bus: Bus) -> None:
    bus.init()
    bus.byte(False, 0x40)  # CGRAM character 0: a cross
    for row in (0x00, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x00, 0x00):
        bus.byte(True, row)
    bus.byte(False, 0x80)
    bus.text("2\x083")  # 0x08 mirrors character 0
    bus.flush()
    frame = bus.device.screen(bus.t).frame_at(None)
    assert frame.text == "2\x003\n"
    svg = draw(frame)
    assert ElementTree.fromstring(svg).find("{http://www.w3.org/2000/svg}title") is not None
    assert "2\\x003" in svg


def test_font() -> None:
    assert glyph(ord("A")) == (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11, 0)
    assert columns(ord("I")) == (0x00, 0x41, 0x7F, 0x41, 0x00)
    assert character(0x5C) == "¥" and code_of("¥") == 0x5C and code_of("\\") is None
    assert character(0xB1) == "ｱ"
    glyphs = [glyph(code) for code in range(0x20, 0x80)]
    assert len(set(glyphs)) == len(glyphs)  # every character reads back uniquely


def test_lcd_expectation_needs_firmware(make_project: MakeProject) -> None:
    tests = _tests("{ display: LCD1, match: Hi }")
    diag = Diagnostics()
    load_design(make_project(LCD, tests), diag)
    assert "display_needs_firmware" in [e.rule for e in diag.errors]
