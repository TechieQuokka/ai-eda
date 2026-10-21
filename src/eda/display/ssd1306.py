"""A monochrome OLED's SSD1306 controller, fed by the I2C transfers the firmware makes.

Follows the Solomon Systech SSD1306 datasheet (rev 1.1): control bytes (Co, D/C), the
fundamental, addressing, hardware-configuration, timing, and charge-pump commands with
their parameters, the three memory addressing modes, and the mapping of display RAM to
the panel (segment remap A0/A1, COM scan direction C0/C8, start line, display offset,
multiplex ratio, COM pin configuration). Scrolling is accepted but not drawn.

What the panel shows is read back as text by matching the 5x8 font of `font5x8` at any
pixel position (the font the example firmware draws with), and lit pixels can be counted
in a region.
"""

from dataclasses import dataclass, field
from html import escape

from pydantic import JsonValue

from eda.adapters.rp2040 import I2cTransfer
from eda.display import Frame, Recorded
from eda.display.digital import Segments
from eda.display.font5x8 import character, columns
from eda.model import ResolvedCircuit, ResolvedPart
from eda.schema.part import Ssd1306DisplaySpec
from eda.units import format_quantity

# Commands followed by parameter bytes, and how many.
PARAMETERS = {
    0x81: 1,  # contrast
    0x20: 1,  # memory addressing mode
    0x21: 2,  # column address range
    0x22: 2,  # page address range
    0xA8: 1,  # multiplex ratio
    0xD3: 1,  # display offset
    0xD5: 1,  # clock divide / oscillator
    0xD9: 1,  # pre-charge period
    0xDA: 1,  # COM pins configuration
    0xDB: 1,  # VCOMH level
    0x8D: 1,  # charge pump
    0x26: 6,  # right horizontal scroll
    0x27: 6,  # left horizontal scroll
    0x29: 5,  # vertical and right scroll
    0x2A: 5,  # vertical and left scroll
    0xA3: 2,  # vertical scroll area
}
COLUMNS = 128  # display RAM is always 128 columns x 8 pages
PAGES = 8


@dataclass
class Controller:
    """SSD1306 state after reset, changed one command or data byte at a time."""

    ram: bytearray = field(default_factory=lambda: bytearray(COLUMNS * PAGES))
    on: bool = False
    pump: bool = False
    invert: bool = False
    entire: bool = False
    contrast: int = 0x7F
    mode: int = 2  # 0 horizontal, 1 vertical, 2 page addressing
    col_start: int = 0
    col_end: int = COLUMNS - 1
    page_start: int = 0
    page_end: int = PAGES - 1
    col: int = 0
    page: int = 0
    start_line: int = 0
    offset: int = 0
    remap: bool = False  # A1: column 127 drives SEG0
    com_reverse: bool = False  # C8: scan from COM[N-1] to COM0
    mux: int = 63
    com_alternative: bool = True
    scrolled: bool = False
    pending: list[int] = field(default_factory=lambda: list[int]())

    def transfer(self, data: tuple[int, ...]) -> None:
        """One I2C write after the address: control bytes and what they announce."""
        i = 0
        while i < len(data):
            control = data[i]
            i += 1
            is_data = bool(control & 0x40)
            if control & 0x80:  # Co = 1: one byte, then another control byte
                if i < len(data):
                    self._byte(is_data, data[i])
                    i += 1
                continue
            for value in data[i:]:  # Co = 0: the rest of the transfer
                self._byte(is_data, value)
            break

    def _byte(self, is_data: bool, value: int) -> None:
        if is_data:
            self._data(value)
            return
        self.pending.append(value)
        if len(self.pending) - 1 >= PARAMETERS.get(self.pending[0], 0):
            command, *params = self.pending
            self.pending = []
            self._command(command, params)

    def _command(self, command: int, params: list[int]) -> None:
        if command in (0xAE, 0xAF):
            self.on = command == 0xAF
        elif command == 0x8D:
            self.pump = bool(params[0] & 0x04)
        elif command == 0x81:
            self.contrast = params[0]
        elif command == 0x20:
            self.mode = params[0] & 0x03 if params[0] & 0x03 != 3 else self.mode
        elif command == 0x21:
            self.col_start, self.col_end = params[0] & 0x7F, params[1] & 0x7F
            self.col = self.col_start
        elif command == 0x22:
            self.page_start, self.page_end = params[0] & 0x07, params[1] & 0x07
            self.page = self.page_start
        elif 0xB0 <= command <= 0xB7:
            self.page = command & 0x07
        elif command <= 0x0F:
            self.col = (self.col & 0xF0) | command
        elif command <= 0x1F:
            self.col = (self.col & 0x0F) | ((command & 0x07) << 4)
        elif 0x40 <= command <= 0x7F:
            self.start_line = command & 0x3F
        elif command in (0xA0, 0xA1):
            self.remap = command == 0xA1
        elif command in (0xC0, 0xC8):
            self.com_reverse = command == 0xC8
        elif command in (0xA4, 0xA5):
            self.entire = command == 0xA5
        elif command in (0xA6, 0xA7):
            self.invert = command == 0xA7
        elif command == 0xA8:
            self.mux = max(15, params[0] & 0x3F)
        elif command == 0xD3:
            self.offset = params[0] & 0x3F
        elif command == 0xDA:
            self.com_alternative = bool(params[0] & 0x10)
        elif command == 0x2F:
            self.scrolled = True
        elif command == 0x2E:
            self.scrolled = False

    def _data(self, value: int) -> None:
        self.ram[self.page * COLUMNS + self.col] = value
        if self.mode == 0:
            self.col += 1
            if self.col > self.col_end:
                self.col = self.col_start
                self.page = self.page + 1 if self.page < self.page_end else self.page_start
        elif self.mode == 1:
            self.page += 1
            if self.page > self.page_end:
                self.page = self.page_start
                self.col = self.col + 1 if self.col < self.col_end else self.col_start
        else:
            self.col = (self.col + 1) % COLUMNS

    def pixels(self, width: int, height: int, panel_alternative: bool) -> list[list[bool]]:
        """The panel, row by row from the top: which pixels are lit (ignoring on/off)."""
        rows = self.mux + 1
        image: list[list[bool]] = []
        for y in range(height):
            if self.com_alternative != panel_alternative:
                y = (y % 2) * (height // 2) + y // 2  # rows wired for the other COM layout
            com = y if self.com_reverse else rows - 1 - y
            if not 0 <= com < rows:
                image.append([False] * width)  # beyond the multiplex ratio: not driven
                continue
            line = (com + self.offset + self.start_line) % 64
            page, bit = divmod(line, 8)
            row: list[bool] = []
            for x in range(width):
                column = x if self.remap else COLUMNS - 1 - x
                lit = bool(self.ram[page * COLUMNS + column] >> bit & 1)
                row.append(True if self.entire else lit != self.invert)
            image.append(row)
        return image


# --- reading text back ----------------------------------------------------------------------

_GLYPHS: dict[tuple[int, ...], str] = {
    shape: character(code) for code in range(0x21, 0x80) if (shape := columns(code)) is not None
}


def read_text(image: list[list[bool]]) -> str:
    """The text drawn on a pixel image in the 5x8 font: one line per row of characters,
    spaces from the gaps (a character cell is 6 pixels wide)."""
    height = len(image)
    width = len(image[0]) if image else 0
    if height < 8 or width < 5:
        return ""
    # column bytes of every 8-pixel-high window: windows[y][x], bit 0 = row y
    windows = [
        [sum(1 << k for k in range(8) if image[y + k][x]) for x in range(width)]
        for y in range(height - 7)
    ]
    found: dict[int, list[tuple[int, str]]] = {}
    for y, window in enumerate(windows):
        for x in range(width - 4):
            char = _GLYPHS.get(tuple(window[x : x + 5]))
            if char is None:
                continue
            if (x > 0 and window[x - 1]) or (x + 5 < width and window[x + 5]):
                continue  # part of something wider
            if y > 0 and any(image[y - 1][x : x + 5]):
                continue  # part of something taller
            found.setdefault(y, []).append((x, char))
    lines: list[int] = []
    for y in sorted(found, key=lambda y: (-len(found[y]), y)):
        if all(abs(y - other) >= 8 for other in lines):
            lines.append(y)
    text: list[str] = []
    for y in sorted(lines):
        line = ""
        last: int | None = None
        for x, char in sorted(found[y]):
            if last is not None:
                if x - last < 5:
                    continue
                line += " " * max(0, round((x - last - 6) / 6))
            line += char
            last = x
        text.append(line)
    return "\n".join(text)


def lit_in(image: list[list[bool]], region: tuple[int, int, int, int]) -> int:
    """Lit pixels in the inclusive rectangle (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = region
    return sum(row[x0 : x1 + 1].count(True) for row in image[y0 : y1 + 1])


def pack(image: list[list[bool]]) -> str:
    """Row-major bits, most significant first, as hex (what the viewer draws)."""
    bits = [pixel for row in image for pixel in row]
    data = bytes(
        sum(1 << (7 - k) for k in range(8) if i + k < len(bits) and bits[i + k])
        for i in range(0, len(bits), 8)
    )
    return data.hex()


def unpack(hexed: str, width: int, height: int) -> list[list[bool]]:
    data = bytes.fromhex(hexed)
    bits = [bool(byte >> (7 - k) & 1) for byte in data for k in range(8)]
    return [bits[y * width : (y + 1) * width] for y in range(height)]


# --- the module in a co-simulation ------------------------------------------------------------


class Ssd1306Device:
    kind = "ssd1306"

    def __init__(self, part: ResolvedPart, circuit: ResolvedCircuit, bus: int | None) -> None:
        spec = part.spec.display
        assert isinstance(spec, Ssd1306DisplaySpec)
        self.ref = part.ref
        self.spec = spec
        chosen = part.values.get("address")
        self.address = int(str(chosen), 16) if chosen is not None else spec.address
        self.bus = bus
        self.supply = (
            circuit.net_of(part.ref, spec.pins["vcc"]),
            circuit.net_of(part.ref, spec.pins["gnd"]),
        )
        self.nets = sorted({net for net in self.supply if net is not None})
        self.controller = Controller()
        self.powered = False
        self.frames: list[Frame] = []
        self._shown: JsonValue = None
        self.written = False
        self.notes: dict[str, str] = {}

    @property
    def vih(self) -> float:
        """SDA/SCL input threshold: 0.8 x the controller supply."""
        return 0.8 * self.spec.volts("logic")

    def window(
        self,
        start: float,
        end: float,
        gpio: dict[str, Segments],
        volts: dict[str, float],
        transfers: tuple[I2cTransfer, ...] = (),
    ) -> None:
        del gpio  # an I2C device sees transfers, not pin states
        vcc, gnd = (volts.get(net, 0.0) if net else 0.0 for net in self.supply)
        supply = vcc - gnd
        powered = supply >= self.spec.volts("supply_min")
        if supply > self.spec.volts("supply_max"):
            self.notes.setdefault(
                "display_supply",
                f"supply {format_quantity(supply, 'V')} is above {self.spec.supply_max}",
            )
        if powered != self.powered:
            self.controller = Controller()  # power-on reset, or the contents are lost
            if self.powered:
                self.notes.setdefault(
                    "display_supply",
                    f"supply fell to {format_quantity(supply, 'V')} at "
                    f"{format_quantity(end, 's')}: the display lost its contents",
                )
        self.powered = powered
        self._record(start)
        for transfer in transfers:
            if transfer.bus != self.bus or transfer.address != self.address:
                continue
            if not transfer.acked or transfer.read:
                continue
            if self.powered:
                self.written = True
                self.controller.transfer(transfer.data)
                self._record(max(transfer.end, transfer.time))

    def _visible(self) -> bool:
        c = self.controller
        return self.powered and c.on and (c.pump or not self.spec.charge_pump)

    def _record(self, time: float) -> None:
        c = self.controller
        visible = self._visible()
        width, height = self.spec.width, self.spec.height
        image = c.pixels(width, height, self.spec.com_alternative) if visible else None
        state: dict[str, JsonValue] = {
            "powered": self.powered,
            "on": visible,
            "contrast": c.contrast,
            "pixels": pack(image) if image is not None else None,
        }
        if state == self._shown:
            return
        self._shown = state
        text = read_text(image) if image is not None else ""
        self.frames.append(Frame(time=time, text=text, state=state))

    def issues(self) -> list[tuple[str, str]]:
        found = [(rule, f"{self.ref}: {message}") for rule, message in self.notes.items()]
        c = self.controller
        if self.bus is None:
            found.append(
                (
                    "display_bus",
                    f"{self.ref}: SDA/SCL are not on a firmware I2C bus (I2Cn_SDA / I2Cn_SCL "
                    "in firmware.yaml), so nothing reaches the controller",
                )
            )
        elif not self.written:
            found.append(
                (
                    "display_bus",
                    f"{self.ref}: the firmware never wrote to address 0x{self.address:02X} on "
                    f"I2C{self.bus}",
                )
            )
        elif not self._visible() and self.powered:
            why = (
                "the display is off (AE; AF turns it on)"
                if not c.on
                else "the charge pump is off: the panel gets no voltage (send 8D 14 before AF)"
            )
            found.append(("display_dark", f"{self.ref}: the panel stays dark: {why}"))
        if not self.powered and not any(f.state.get("powered") for f in self.frames):
            found.append(
                (
                    "display_unpowered",
                    f"{self.ref}: the supply never reached {self.spec.supply_min}",
                )
            )
        if c.com_alternative != self.spec.com_alternative:
            layout = "alternative (DA 12)" if self.spec.com_alternative else "sequential (DA 02)"
            found.append(
                (
                    "display_layout",
                    f"{self.ref}: the panel is wired for the {layout} COM configuration; the "
                    "firmware set the other one, so the rows come out interleaved",
                )
            )
        if c.scrolled:
            found.append(("display_scroll", f"{self.ref}: scrolling is active; it is not drawn"))
        return found

    def screen(self, stop: float) -> Recorded:
        if not self.frames:
            self._record(stop)
        width, height = self.spec.width, self.spec.height
        meta: dict[str, JsonValue] = {"width": width, "height": height}
        return Recorded(
            self.ref, self.kind, self.frames, lambda frame: draw(frame, width, height), meta
        )


def frame_image(frame: Frame, width: int, height: int) -> list[list[bool]]:
    pixels = frame.state.get("pixels")
    if not isinstance(pixels, str):
        return [[False] * width for _ in range(height)]
    return unpack(pixels, width, height)


# --- drawing -------------------------------------------------------------------------------

_SCALE, _BORDER = 4, 8


def draw(frame: Frame, width: int, height: int) -> str:
    """The panel as an SVG: black glass, lit pixels in pale blue."""
    image = frame_image(frame, width, height)
    w, h = width * _SCALE + 2 * _BORDER, height * _SCALE + 2 * _BORDER
    contrast = frame.state.get("contrast")
    level = 0.45 + 0.55 * (int(contrast) / 255 if isinstance(contrast, int) else 0.5)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" rx="4" fill="#05070a"/>',
        f'<g fill="#bfe8ff" fill-opacity="{level:.2f}">',
    ]
    for y, row in enumerate(image):
        x = 0
        while x < width:
            if not row[x]:
                x += 1
                continue
            run = x
            while run < width and row[run]:
                run += 1
            parts.append(
                f'<rect x="{_BORDER + x * _SCALE}" y="{_BORDER + y * _SCALE}" '
                f'width="{(run - x) * _SCALE}" height="{_SCALE}"/>'
            )
            x = run
    parts.append(f"</g><title>{escape(frame.text)}</title></svg>")
    return "".join(parts)
