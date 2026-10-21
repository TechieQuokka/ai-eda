"""A character LCD's HD44780 controller, fed by the logic levels of its bus pins.

The controller follows the Hitachi HD44780U datasheet: instructions (clear, home, entry
mode, display control, shift, function set, CGRAM / DDRAM address) and data writes, the
8-bit and 4-bit interfaces (D7-D4, high nibble first), busy times at the typical 270 kHz
oscillator (37 us; 41 us for data; 1.52 ms for clear and home), the internal reset after
power-up, DDRAM addressing for one or two lines (and four-row modules), CGRAM characters,
and the display shift. A write while the controller is busy is ignored, as on the chip.

The bus is checked against the write timing at 5 V: E high for at least 230 ns and one
cycle at least 500 ns, RS/RW set up 40 ns before E rises, data set up 80 ns before E
falls and held 10 ns after. Reading (RW high) is not modelled.
"""

import re
from dataclasses import dataclass, field
from html import escape

from pydantic import JsonValue

from eda.adapters.rp2040 import I2cTransfer
from eda.display import Frame, Recorded
from eda.display.digital import Edge, Inputs, Segments
from eda.display.font5x8 import character, glyph
from eda.model import ResolvedCircuit, ResolvedPart
from eda.schema.part import HD44780_ROLES, Hd44780DisplaySpec
from eda.units import format_quantity, parse_quantity

EXEC_S = 37e-6
EXEC_DATA_S = 41e-6  # 37 us + t_ADD
EXEC_LONG_S = 1.52e-3  # clear display, return home
PWEH_S = 230e-9  # E pulse width (high)
TCYCE_S = 500e-9  # E cycle time
TAS_S = 40e-9  # RS, R/W set-up time before E rises
TDSW_S = 80e-9  # data set-up time before E falls
TH_S = 10e-9  # data hold time after E falls
LINE = 40  # DDRAM positions per line in two-line mode
SIGNALS = ("rs", "rw", "e", "d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7")
DATA = ("d7", "d6", "d5", "d4", "d3", "d2", "d1", "d0")


@dataclass
class Controller:
    """HD44780 state, written one instruction or data byte at a time."""

    columns: int
    rows: int
    ddram: list[int] = field(default_factory=lambda: [0x20] * 0x80)
    cgram: list[int] = field(default_factory=lambda: [0] * 64)
    address: int = 0
    in_cgram: bool = False
    increment: bool = True
    shift_on_write: bool = False
    on: bool = False
    cursor: bool = False
    blink: bool = False
    eight_bit: bool = True
    two_lines: bool = False
    shift: int = 0  # display shift, in positions to the left
    high_nibble: int | None = None
    busy_until: float = 0.0
    last_write: float = 0.0
    last_exec: float = EXEC_S

    def write(self, time: float, rs: bool, value: int) -> str | None:
        """One bus write (a byte, or a nibble in 4-bit mode). Returns why it was ignored."""
        if not self.eight_bit:
            if self.high_nibble is None:
                self.high_nibble = value & 0x0F
                return None
            value = (self.high_nibble << 4) | (value & 0x0F)
            self.high_nibble = None
        if time < self.busy_until:
            waited = format_quantity(time - self.last_write, "s")
            needed = format_quantity(self.last_exec, "s")
            what = f"data 0x{value:02X}" if rs else f"instruction 0x{value:02X}"
            return f"{what} came {waited} after the previous write, which needs {needed}"
        self.last_write = time
        self.last_exec = EXEC_DATA_S if rs else self._execute(value)
        if rs:
            self._data(value)
        self.busy_until = time + self.last_exec
        return None

    def _execute(self, value: int) -> float:
        if value & 0x80:  # set DDRAM address
            self.address, self.in_cgram = self._ddram_address(value & 0x7F), False
        elif value & 0x40:  # set CGRAM address
            self.address, self.in_cgram = value & 0x3F, True
        elif value & 0x20:  # function set
            self.eight_bit = bool(value & 0x10)
            self.two_lines = bool(value & 0x08)
            self.high_nibble = None
        elif value & 0x10:  # cursor or display shift
            step = 1 if value & 0x04 else -1
            if value & 0x08:
                self.shift = (self.shift - step) % self._span
            else:
                self._move(step)
        elif value & 0x08:  # display on/off control
            self.on, self.cursor, self.blink = (bool(value & bit) for bit in (0x04, 0x02, 0x01))
        elif value & 0x04:  # entry mode set
            self.increment, self.shift_on_write = bool(value & 0x02), bool(value & 0x01)
        elif value & 0x02:  # return home
            self.address, self.in_cgram, self.shift = 0, False, 0
            return EXEC_LONG_S
        elif value & 0x01:  # clear display
            self.ddram = [0x20] * 0x80
            self.address, self.in_cgram, self.shift, self.increment = 0, False, 0, True
            return EXEC_LONG_S
        return EXEC_S

    def _data(self, value: int) -> None:
        step = 1 if self.increment else -1
        if self.in_cgram:
            self.cgram[self.address] = value
            self.address = (self.address + step) % 64
            return
        self.ddram[self.address] = value
        self._move(step)
        if self.shift_on_write:
            self.shift = (self.shift + step) % self._span

    @property
    def _span(self) -> int:
        return LINE if self.two_lines else 2 * LINE

    def _ddram_address(self, address: int) -> int:
        if not self.two_lines:
            return address % (2 * LINE)
        line = 0x40 if address >= 0x40 else 0
        return line + min(address - line, LINE - 1)

    def _move(self, step: int) -> None:
        if self.in_cgram:
            self.address = (self.address + step) % 64
        elif self.two_lines:
            line, offset = (
                (0x40, self.address - 0x40) if self.address >= 0x40 else (0, self.address)
            )
            offset += step
            if offset >= LINE:  # end of line 1 continues on line 2 and back
                line, offset = (0 if line else 0x40), 0
            elif offset < 0:
                line, offset = (0 if line else 0x40), LINE - 1
            self.address = line + offset
        else:
            self.address = (self.address + step) % (2 * LINE)

    def cell(self, row: int, column: int) -> int | None:
        """DDRAM address shown at a row and column, or None for a dark row."""
        if not self.two_lines:
            if row > 0:
                return None  # one-line mode drives the first row only
            return (column + self.shift) % (2 * LINE)
        line = 0x40 if row % 2 else 0
        offset = (row // 2) * self.columns + column
        return line + (offset + self.shift) % LINE

    def codes(self) -> list[list[int]]:
        return [
            [
                self.ddram[a] if (a := self.cell(r, c)) is not None else 0x20
                for c in range(self.columns)
            ]
            for r in range(self.rows)
        ]

    def cursor_cell(self) -> tuple[int, int] | None:
        if self.in_cgram or not (self.cursor or self.blink):
            return None
        for row in range(self.rows):
            for column in range(self.columns):
                if self.cell(row, column) == self.address:
                    return row, column
        return None


class Hd44780Device:
    """One LCD part during a co-simulation: levels in, frames out."""

    kind = "hd44780"

    def __init__(
        self, part: ResolvedPart, circuit: ResolvedCircuit, vio: float, driven: set[str]
    ) -> None:
        spec = part.spec.display
        assert isinstance(spec, Hd44780DisplaySpec)
        self.ref = part.ref
        self.spec = spec
        self.scheme = str(part.values.get("backlight", "green"))
        nets = {
            role: circuit.net_of(part.ref, pin) if (pin := spec.pins.get(role)) else None
            for role in HD44780_ROLES
        }
        self.supply = (nets["vdd"], nets["vss"], nets["v0"])
        self.inputs = Inputs(
            {role: nets[role] for role in SIGNALS}, spec.volts("vih"), spec.volts("vil"), vio
        )
        self.nets = sorted({net for net in nets.values() if net is not None})
        self.controller = Controller(spec.columns, spec.rows)
        self.levels: dict[str, bool] = {}
        self.changed: dict[str, float] = {}
        self.e_rise: float | None = None
        self.e_fall: float | None = None
        self.powered = False
        self.vlcd = 0.0
        self.frames: list[Frame] = []
        self._shown: JsonValue = None
        self.notes: dict[str, list[str]] = {}  # rule → messages, first ones first
        self.counts: dict[str, int] = {}
        for role in ("e", "rs", "d4", "d5", "d6", "d7"):
            net = nets[role]
            if net is not None and net not in driven:
                self._note(
                    "display_bus",
                    f"{role.upper()} is on net {net}, which no firmware GPIO drives: it is "
                    "sampled once per step, too slowly for the bus timing",
                )

    # --- co-simulation ---------------------------------------------------------------

    def window(
        self,
        start: float,
        end: float,
        gpio: dict[str, Segments],
        volts: dict[str, float],
        transfers: tuple[I2cTransfer, ...] = (),
    ) -> None:
        del transfers  # a parallel bus: only pin levels matter
        vdd, vss, v0 = (volts.get(net, 0.0) if net else 0.0 for net in self.supply)
        supply = vdd - vss
        self.vlcd = vdd - v0
        powered = supply >= self.spec.volts("supply_min")
        if supply > self.spec.volts("supply_max"):
            self._note(
                "display_supply",
                f"supply {format_quantity(supply, 'V')} at {format_quantity(end, 's')} is "
                f"above {self.spec.supply_max}",
            )
        if powered and not self.powered:
            self.controller = Controller(self.spec.columns, self.spec.rows)
            self.controller.busy_until = start + parse_quantity(self.spec.reset, "s")
            self.controller.last_write, self.controller.last_exec = (
                start,
                self.controller.busy_until - start,
            )
        elif self.powered and not powered:
            self._note(
                "display_supply",
                f"supply fell to {format_quantity(supply, 'V')} at {format_quantity(end, 's')} "
                f"(below {self.spec.supply_min}): the display lost its contents",
            )
        self.powered = powered
        self._record(start)  # power and contrast as of this window
        for edge in self.inputs.window(start, gpio, volts):
            self._edge(edge)

    def _edge(self, edge: Edge) -> None:
        time, role, level = edge
        first = role not in self.levels
        self.levels[role] = level
        if first:
            self.changed[role] = time
            return
        if role != "e":
            if self.e_fall is not None and time - self.e_fall < TH_S:
                self._timing(
                    f"{role.upper()} changed {_ns(time - self.e_fall)} after E fell (hold 10 ns)",
                    time,
                )
            self.changed[role] = time
            return
        if level:
            for name in ("rs", "rw"):
                setup = time - self.changed.get(name, -1.0)
                if setup < TAS_S:
                    self._timing(
                        f"{name.upper()} set up {_ns(setup)} before E rose (needs 40 ns)", time
                    )
            if self.e_rise is not None and time - self.e_rise < TCYCE_S:
                self._timing(f"E cycle of {_ns(time - self.e_rise)} (needs 500 ns)", time)
            self.e_rise = time
        else:
            if self.e_rise is not None and time - self.e_rise < PWEH_S:
                self._timing(f"E high for {_ns(time - self.e_rise)} (needs 230 ns)", time)
            data = DATA if self.controller.eight_bit else DATA[:4]
            for name in data:
                setup = time - self.changed.get(name, -1.0)
                if setup < TDSW_S:
                    self._timing(
                        f"{name.upper()} set up {_ns(setup)} before E fell (needs 80 ns)", time
                    )
            self.e_fall = time
            self._latch(time)
        self.changed[role] = time

    def _latch(self, time: float) -> None:
        if not self.powered:
            self._note(
                "display_unpowered",
                f"a write at {format_quantity(time, 's')} reached an unpowered display",
            )
            return
        if self.levels.get("rw"):
            self._note(
                "display_read",
                f"RW was high at {format_quantity(time, 's')}: reads are not modelled (and "
                "the LCD would drive its data pins to its own supply)",
            )
            return
        bits = DATA if self.controller.eight_bit else DATA[:4]
        value = sum(
            1 << (len(bits) - 1 - i) for i, name in enumerate(bits) if self.levels.get(name)
        )
        ignored = self.controller.write(time, bool(self.levels.get("rs")), value)
        if ignored is not None:
            self.counts["display_busy"] = self.counts.get("display_busy", 0) + 1
            self._note("display_busy", f"at {format_quantity(time, 's')}: {ignored}")
        self._record(time)

    def _timing(self, message: str, time: float) -> None:
        self.counts["display_timing"] = self.counts.get("display_timing", 0) + 1
        self._note("display_timing", f"at {format_quantity(time, 's')}: {message}")

    def _note(self, rule: str, message: str) -> None:
        notes = self.notes.setdefault(rule, [])
        if len(notes) < 3 and message not in notes:
            notes.append(message)

    # --- frames ------------------------------------------------------------------------

    def _record(self, time: float) -> None:
        c = self.controller
        visible = self.powered and c.on
        contrast = _contrast(self.vlcd, self.spec)
        state: dict[str, JsonValue] = {
            "powered": self.powered,
            "on": c.on,
            "rows": [list[JsonValue](row) for row in c.codes()],
            "cursor": list[JsonValue](cell) if (cell := c.cursor_cell()) and visible else None,
            "underline": c.cursor,
            "blink": c.blink,
            "contrast": contrast,
            "cgram": list[JsonValue](c.cgram) if any(c.cgram) else None,
        }
        if state == self._shown:
            return
        self._shown = state
        readable = visible and contrast != "faint"
        text = "\n".join(
            "".join(character(code) for code in row).rstrip() if readable else ""
            for row in c.codes()
        )
        self.frames.append(Frame(time=time, text=text, state=state))

    def issues(self) -> list[tuple[str, str]]:
        """(rule, message) warnings about how the display was driven."""
        found: list[tuple[str, str]] = []
        for rule, messages in self.notes.items():
            count = self.counts.get(rule)
            more = f" ({count} times)" if count and count > 1 else ""
            found.append((rule, f"{self.ref}: " + "; ".join(messages) + more))
        for role, volts in self.inputs.undefined.items():
            found.append(
                (
                    "display_logic_level",
                    f"{self.ref}.{role} saw {format_quantity(volts, 'V')}, between VIL "
                    f"{self.spec.vil} and VIH {self.spec.vih}: the level is undefined",
                )
            )
        if not self.powered and not any(f.state.get("powered") for f in self.frames):
            found.append(
                (
                    "display_unpowered",
                    f"{self.ref}: the supply never reached {self.spec.supply_min}",
                )
            )
        if self.frames and self.powered:
            contrast = _contrast(self.vlcd, self.spec)
            if contrast != "good":
                why = (
                    f"below {self.spec.vlcd_visible}: nothing is visible"
                    if contrast == "faint"
                    else f"above {self.spec.vlcd_dark}: the unlit dots show as dark boxes"
                )
                found.append(
                    (
                        "display_contrast",
                        f"{self.ref}: VDD-V0 = {format_quantity(self.vlcd, 'V')}, {why}",
                    )
                )
        return found

    def screen(self, stop: float) -> Recorded:
        if not self.frames:
            self._record(stop)
        codes = {code for frame in self.frames for row in _rows(frame) for code in row}
        font: dict[str, JsonValue] = {
            str(code): list[JsonValue](rows) for code in sorted(codes) if (rows := glyph(code))
        }
        meta: dict[str, JsonValue] = {
            "columns": self.spec.columns,
            "rows": self.spec.rows,
            "scheme": self.scheme,
            "colors": dict[str, JsonValue](_scheme(self.scheme)),
            "font": font,
        }
        scheme = self.scheme
        return Recorded(self.ref, self.kind, self.frames, lambda frame: draw(frame, scheme), meta)


def _rows(frame: Frame) -> list[list[int]]:
    rows = frame.state.get("rows")
    assert isinstance(rows, list)
    return [
        [int(code) for code in row if isinstance(code, int)]
        for row in rows
        if isinstance(row, list)
    ]


def _contrast(vlcd: float, spec: Hd44780DisplaySpec) -> str:
    if vlcd < spec.volts("vlcd_visible"):
        return "faint"
    if vlcd > spec.volts("vlcd_dark"):
        return "dark"
    return "good"


def _ns(seconds: float) -> str:
    return f"{seconds * 1e9:.0f} ns"


# --- drawing -------------------------------------------------------------------------------

_DOT, _GAP, _CELL_GAP, _MARGIN = 3.0, 0.6, 3.0, 12.0


# glass, lit dot, unlit dot, unlit dot at high contrast, lit dot at low contrast
_SCHEMES = {
    "blue": ("#1f4fd1", "#f2f6ff", "#3a66de", "#5a80ea", "#7f9cf0"),
    "white": ("#e8ece4", "#20241c", "#d9ded4", "#b9bfb2", "#a9afa2"),
    "green": ("#9fbf3b", "#1d2a12", "#a9c94a", "#6f8f22", "#7e9a3a"),
}


def _scheme(name: str) -> dict[str, str]:
    glass, lit, unlit, dark, faint = _SCHEMES.get(name, _SCHEMES["green"])
    return {"glass": glass, "lit": lit, "unlit": unlit, "dark": dark, "faint": faint}


def draw(frame: Frame, scheme: str = "green") -> str:
    """The LCD as an SVG in its backlight's colours, with a cursor if shown."""
    rows = _rows(frame)
    state = frame.state
    cgram = state.get("cgram")
    on = bool(state.get("powered")) and bool(state.get("on"))
    contrast = state.get("contrast")
    pitch = _DOT + _GAP
    cell_w, cell_h = 5 * pitch + _CELL_GAP, 8 * pitch + _CELL_GAP
    columns = len(rows[0]) if rows else 16
    width = 2 * _MARGIN + columns * cell_w
    height = 2 * _MARGIN + len(rows) * cell_h
    colors = _scheme(scheme)
    ink = colors["faint"] if contrast == "faint" else colors["lit"]
    unlit = colors["dark"] if contrast == "dark" else colors["unlit"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}" height="{height:g}" '
        f'viewBox="0 0 {width:g} {height:g}">',
        f'<rect width="{width:g}" height="{height:g}" rx="4" fill="{colors["glass"]}"/>',
    ]
    cursor = state.get("cursor")
    for r, row in enumerate(rows):
        for c, code in enumerate(row):
            x0, y0 = _MARGIN + c * cell_w, _MARGIN + r * cell_h
            dots = _dots(code, cgram) if on else (0,) * 8
            if cursor == [r, c] and state.get("underline"):
                dots = (*dots[:7], 0x1F)
            for y, bits in enumerate(dots):
                for x in range(5):
                    lit = bool(bits & (1 << (4 - x)))
                    fill = ink if lit else unlit
                    parts.append(
                        f'<rect x="{x0 + x * pitch:g}" y="{y0 + y * pitch:g}" width="{_DOT:g}" '
                        f'height="{_DOT:g}" fill="{fill}"/>'
                    )
    parts.append(f"<title>{escape(_printable(frame.text))}</title></svg>")
    return "".join(parts)


def _printable(text: str) -> str:
    """CGRAM characters (\\x00-\\x07) spelled out: XML does not allow control characters."""
    return re.sub(r"[\x00-\x08\x0b-\x1f]", lambda m: f"\\x{ord(m[0]):02x}", text)


def _dots(code: int, cgram: JsonValue) -> tuple[int, ...]:
    if code < 0x10:
        if not isinstance(cgram, list):
            return (0,) * 8
        base = (code & 0x07) * 8
        return tuple(int(v) & 0x1F if isinstance(v, int) else 0 for v in cgram[base : base + 8])
    found = glyph(code)
    return found if found is not None else (0x1F, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1F, 0)
