import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from eda.schema.common import Ident, StrictModel
from eda.units import CANONICAL, parse_quantity


class ParamSpec(StrictModel):
    """A part parameter: either a quantity with `unit` or one of `choices`.

    Quantities may be limited: `positive: true` (greater than zero, e.g. a resistance) and
    inclusive `min` / `max` in the same unit (e.g. a wiper position 0 %..100 %)."""

    description: str = ""
    unit: str | None = None
    choices: list[str] | None = None
    default: str | None = None
    positive: bool = False
    min: str | None = None
    max: str | None = None

    @model_validator(mode="after")
    def _unit_xor_choices(self) -> Self:
        if (self.unit is None) == (self.choices is None):
            raise ValueError("exactly one of 'unit' or 'choices' is required")
        if self.unit is not None and self.unit not in CANONICAL:
            raise ValueError(f"unknown unit {self.unit!r}")
        if self.choices is not None:
            if self.positive or self.min is not None or self.max is not None:
                raise ValueError("positive / min / max apply to quantities, not choices")
            if self.default is not None and self.default not in self.choices:
                raise ValueError(f"default {self.default!r} is not one of the choices")
            return self
        bounds = self.bounds  # parses min and max
        if self.default is not None:
            problem = self.out_of_range(parse_quantity(self.default, self.unit or ""))
            if problem:
                raise ValueError(f"default {self.default}: {problem}")
        if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
            raise ValueError("min is greater than max")
        return self

    @property
    def bounds(self) -> tuple[float | None, float | None]:
        unit = self.unit or ""
        low = parse_quantity(self.min, unit) if self.min is not None else None
        high = parse_quantity(self.max, unit) if self.max is not None else None
        return low, high

    def out_of_range(self, value: float) -> str | None:
        """Why `value` is not allowed, or None if it is."""
        low, high = self.bounds
        if self.positive and value <= 0:
            return "must be greater than zero"
        if low is not None and value < low:
            return f"must be at least {self.min}"
        if high is not None and value > high:
            return f"must be at most {self.max}"
        return None


class OptranSpec(StrictModel):
    """Find the DC operating point by a transient run from power-up (ngspice `optran`)
    instead of Newton iteration, for models that do not converge otherwise."""

    step: str = "100ns"
    stop: str = "10us"  # long enough for the part to settle after power-up

    @model_validator(mode="after")
    def _times(self) -> Self:
        step, stop = parse_quantity(self.step, "s"), parse_quantity(self.stop, "s")
        if not 0 < step < stop:
            raise ValueError("optran needs 0 < step < stop")
        return self

    @property
    def seconds(self) -> tuple[float, float]:
        return parse_quantity(self.step, "s"), parse_quantity(self.stop, "s")


class SpiceSpec(StrictModel):
    """How a part becomes SPICE.

    `template` placeholders: `<name>` element name, `<pin:P>` node of pin P,
    `<param:X>` parameter value, `<model>` model name.
    `models` maps a value of `model_param` to a `.model` body such as `D(IS=1e-14)`.
    Currents are measured with 0 V sense sources: a two-terminal part's `current(R1)` flows
    into its first pin; multi-pin parts (subcircuits) set `pin_currents: true` so every pin
    is measured and tests use `current(U1.out)`.
    `include` is a model file relative to the part definition, `.include`d once per netlist.
    `compat: ps` runs ngspice in PSpice-compatibility mode (needed by many vendor models).
    `optran` finds the operating point by a transient run (see OptranSpec).
    """

    prefix: Annotated[str, StringConstraints(pattern=r"^[A-Z]$")]
    template: str
    pin_currents: bool = False
    model_param: str | None = None
    models: dict[str, str] = Field(default_factory=dict[str, str])
    include: str | None = None
    compat: Literal["ps"] | None = None
    optran: OptranSpec | None = None
    # netlist written by code, not by `template`: "mcu" (McuSpec), "display" (DisplaySpec)
    generator: Literal["mcu", "display"] | None = None
    # `<choice:P>` in the template becomes choice_values[P][value of P], e.g. a switch's
    # resistance for state open / pressed. `<choice:P.T>` reads the table `P.T` instead, so
    # one parameter can set several elements (the two throws of a changeover switch).
    choice_values: dict[str, dict[str, str]] = Field(default_factory=dict[str, dict[str, str]])


class KicadSymbolSpec(StrictModel):
    """Schematic symbol for `.kicad_sch` export: a KiCad library symbol (`Library:Name`)
    and part pin → symbol pin number(s); pins default to the `pcb.pads` numbers."""

    symbol: str
    pins: dict[str, str | list[str]] | None = None


class PcbSpec(StrictModel):
    """Default footprint (`Library:Name` from the KiCad libraries) and pin → pad numbers."""

    footprint: str
    pads: dict[str, str | list[str]]  # a pin may own several pads (e.g. GND)
    nc: list[str] = Field(default_factory=list[str])  # pads that are meant to stay open
    # Value on the silkscreen and in the BOM (template like symbol.label, which is the
    # default); leaves out simulation-only state such as a switch position.
    value: str | None = None
    # Pads that share a number are joined inside the part (a tactile switch's four legs
    # are two pairs): the router connects the net to one pad of each number and KiCad
    # counts the others as connected through the part (its duplicate-pad jumpers).
    joined_pads: bool = False


class GpioSpec(StrictModel):
    number: int
    functions: list[str]


class McuSpec(StrictModel):
    """Microcontroller facet: I/O voltage, power pins, and the functions of each GPIO.

    `gpio` maps a part pin (e.g. GP15) to its GPIO number and the peripheral functions it
    can take (e.g. UART0_TX, SPI1_SCK, I2C0_SDA, PWM7_B, ADC0). Every GPIO can also be a
    plain digital input or output.
    """

    family: str
    vio: float  # I/O supply voltage in volts
    supply_pin: str  # regulated I/O supply output (modelled as a source)
    ground_pin: str
    drive_ohm: float = 50.0  # output driver resistance for simulation
    pull_ohm: float = 50e3  # internal pull-up / pull-down resistance
    pins_pulled_up: list[str] = Field(default_factory=list[str])  # e.g. RUN, 3V3_EN
    loads: dict[str, float] = Field(default_factory=dict[str, float])  # pin → ohms to ground
    sdk_board: str | None = None  # pico-sdk PICO_BOARD for firmware builds
    flash_bytes: int | None = None  # program memory, for size reports
    ram_bytes: int | None = None
    gpio: dict[str, GpioSpec]


SEGMENT_NAMES = ("a", "b", "c", "d", "e", "f", "g", "dp")


class SegmentDisplaySpec(StrictModel):
    """An LED segment display: one LED per digit and segment, between the segment's pin and
    the digit's common pin (cathode or anode). Generated as SPICE with `generator: display`;
    every LED is measured. The eye averages over `persistence`: a segment counts as lit
    when its mean current over that time reaches `lit` (visible at all) and `ghost` of the
    brightest segment's mean (dimmer ones read as dark: ghosting, leakage)."""

    kind: Literal["segments"]
    common: Literal["cathode", "anode"]
    digits: Annotated[list[str], Field(min_length=1)]  # common pins, left to right
    segments: dict[str, str]  # segment (a-g, dp) → pin
    lit: str = "100uA"
    ghost: str = "30%"
    persistence: str = "20ms"

    @model_validator(mode="after")
    def _segments(self) -> Self:
        unknown = set(self.segments) - set(SEGMENT_NAMES)
        if unknown:
            raise ValueError(f"unknown segments {sorted(unknown)}; use {list(SEGMENT_NAMES)}")
        missing = set(SEGMENT_NAMES[:7]) - set(self.segments)
        if missing:
            raise ValueError(f"segments lack {sorted(missing)}")
        if parse_quantity(self.lit, "A") <= 0 or parse_quantity(self.persistence, "s") <= 0:
            raise ValueError("lit and persistence must be greater than zero")
        if not 0 <= parse_quantity(self.ghost, "%") < 100:
            raise ValueError("ghost must be at least 0 % and below 100 %")
        return self

    @property
    def pins(self) -> list[str]:
        return [*self.digits, *self.segments.values()]


HD44780_ROLES = (
    "rs",
    "rw",
    "e",
    "d0",
    "d1",
    "d2",
    "d3",
    "d4",
    "d5",
    "d6",
    "d7",
    "vdd",
    "vss",
    "v0",
)
HD44780_OPTIONAL = ("d0", "d1", "d2", "d3")  # unused in 4-bit mode (internal pull-ups)


class Hd44780DisplaySpec(StrictModel):
    """A character LCD with an HD44780-compatible controller on its parallel bus (8-bit, or
    4-bit on D4-D7). In a firmware test the co-simulation feeds the controller the logic
    levels of its pins: GPIO-driven pins with the emulator's exact timing, others once per
    step, judged against `vih` / `vil`. `pins` maps each role (rs rw e d0-d7 vdd vss v0)
    to a part pin.

    Supply VDD-VSS must stay within `supply_min`..`supply_max`; after it first reaches
    `supply_min` the controller is busy for `reset` (internal reset). The liquid crystal is
    driven by VDD-V0: below `vlcd_visible` nothing shows, above `vlcd_dark` the unlit dots
    darken too (typical for a 5 V module at 25 C)."""

    kind: Literal["hd44780"]
    columns: Annotated[int, Field(ge=8, le=40)]
    rows: Literal[1, 2, 4]
    pins: dict[str, str]
    vih: str = "2.2V"
    vil: str = "0.6V"
    supply_min: str = "4.5V"
    supply_max: str = "5.5V"
    reset: str = "10ms"
    vlcd_visible: str = "3.0V"
    vlcd_dark: str = "4.8V"

    @model_validator(mode="after")
    def _roles(self) -> Self:
        unknown = set(self.pins) - set(HD44780_ROLES)
        if unknown:
            raise ValueError(f"unknown roles {sorted(unknown)}; use {list(HD44780_ROLES)}")
        missing = set(HD44780_ROLES) - set(HD44780_OPTIONAL) - set(self.pins)
        if missing:
            raise ValueError(f"pins lack the roles {sorted(missing)}")
        for name in ("vih", "vil", "supply_min", "supply_max", "vlcd_visible", "vlcd_dark"):
            parse_quantity(getattr(self, name), "V")
        parse_quantity(self.reset, "s")
        return self

    def volts(self, name: str) -> float:
        return parse_quantity(getattr(self, name), "V")


SSD1306_ROLES = ("scl", "sda", "vcc", "gnd")


class Ssd1306DisplaySpec(StrictModel):
    """A monochrome OLED module with an SSD1306 controller on I2C (write only). In a
    firmware test its controller is an I2C device on the bus the SDA/SCL nets reach: it
    acknowledges `address` (or the part's `address` parameter) while both lines idle above
    its input threshold (0.8 x `logic`, the module's regulated controller supply), decodes
    commands and display RAM writes, and shows the panel as the commands map it.

    `charge_pump`: the module makes the panel voltage with the controller's charge pump,
    so the image appears only after `8D 14`. `com_alternative`: the panel's rows are wired
    for the alternative COM configuration (`DA 12`, 128x64 panels); a different setting
    interleaves the rows, as on the real module."""

    kind: Literal["ssd1306"]
    width: Annotated[int, Field(ge=8, le=128)] = 128
    height: Literal[16, 32, 48, 64] = 64
    pins: dict[str, str]
    address: Annotated[int, Field(ge=0x08, le=0x77)] = 0x3C
    logic: str = "3.3V"
    supply_min: str = "3.0V"
    supply_max: str = "5.5V"
    charge_pump: bool = True
    com_alternative: bool = True

    @model_validator(mode="after")
    def _roles(self) -> Self:
        if set(self.pins) != set(SSD1306_ROLES):
            raise ValueError(f"pins must map exactly the roles {list(SSD1306_ROLES)}")
        for name in ("logic", "supply_min", "supply_max"):
            parse_quantity(getattr(self, name), "V")
        return self

    def volts(self, name: str) -> float:
        return parse_quantity(getattr(self, name), "V")


type DisplaySpec = Annotated[
    SegmentDisplaySpec | Hd44780DisplaySpec | Ssd1306DisplaySpec, Field(discriminator="kind")
]


class SymbolSpec(StrictModel):
    """netlistsvg cell type, part pin → skin port mapping, and label template."""

    cell: str
    ports: dict[str, str]
    label: str = ""


class PartSpec(StrictModel):
    """`parts/<id>.yaml`."""

    id: Ident
    name: str
    category: str
    description: str = ""
    pins: Annotated[list[str], Field(min_length=1)]
    params: dict[Ident, ParamSpec] = Field(default_factory=dict[Ident, ParamSpec])
    spice: SpiceSpec
    symbol: SymbolSpec
    pcb: PcbSpec | None = None
    kicad: KicadSymbolSpec | None = None
    mcu: McuSpec | None = None
    display: DisplaySpec | None = None
    optional_pins: list[str] = Field(default_factory=list[str])  # may stay unconnected
    dc_path: bool = True
    generic: bool = False  # a behavioural model, no real part: the BOM needs an `mpn:`
    datasheet: str | None = None
    source_dir: str | None = Field(
        default=None, description="Directory of the definition file; set by the loader."
    )

    def include_path(self) -> str | None:
        """Absolute path of `spice.include`, resolved against the definition's directory."""
        if self.spice.include is None:
            return None
        base = Path(self.source_dir) if self.source_dir else Path()
        return str((base / self.spice.include).resolve())

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.pins)) != len(self.pins):
            raise ValueError("pin names must be unique")
        if set(self.symbol.ports) != set(self.pins):
            raise ValueError("symbol.ports must map every pin exactly once")
        if self.optional_pins and not set(self.optional_pins) <= set(self.pins):
            raise ValueError("optional_pins must be pins of the part")
        if self.mcu is not None and not set(self.mcu.gpio) <= set(self.pins):
            raise ValueError("mcu.gpio keys must be pins of the part")
        if self.display is not None:
            used = self.display.pins
            unknown = set(used.values() if isinstance(used, dict) else used) - set(self.pins)
            if unknown:
                raise ValueError(f"display names pins the part lacks: {sorted(unknown)}")
        segments = isinstance(self.display, SegmentDisplaySpec)
        if (self.spice.generator == "display") != segments:
            raise ValueError("spice.generator: display goes with a `display: { kind: segments }`")
        if self.pcb is not None and set(self.pcb.pads) != set(self.pins):
            raise ValueError("pcb.pads must map every pin exactly once")
        self._check_template()
        if self.spice.model_param is not None:
            spec = self.params.get(self.spice.model_param)
            if spec is None or spec.choices is None:
                raise ValueError("spice.model_param must name a choices parameter")
            missing = set(spec.choices) - set(self.spice.models)
            if missing:
                raise ValueError(f"spice.models lacks entries for {sorted(missing)}")
        return self

    def _check_template(self) -> None:
        """Every placeholder in spice.template must name something the part has, and the
        template holds element lines only: a dot card such as .control would let a part
        file run commands (ngspice's `shell`) when it is simulated."""
        for number, line in enumerate(self.spice.template.split("\n"), start=1):
            if line.strip().startswith("."):
                raise ValueError(
                    f"spice.template line {number} is a control card ({line.split()[0]}); "
                    "templates hold element lines only"
                )
        for token in re.findall(r"<([^<>\s]*)>", self.spice.template):
            kind, sep, key = token.partition(":")
            if not sep and kind == "name":
                continue
            if not sep and kind == "model":
                if self.spice.model_param is None:
                    raise ValueError("spice.template uses <model> but there is no model_param")
                continue
            if kind == "pin" and key in self.pins:
                continue
            if kind == "param" and key in self.params:
                continue
            if kind == "choice":
                param = key.partition(".")[0]
                spec = self.params.get(param)
                if spec is None or spec.choices is None:
                    raise ValueError(f"<choice:{key}> needs a choices parameter {param!r}")
                missing = set(spec.choices) - set(self.spice.choice_values.get(key, {}))
                if missing:
                    raise ValueError(f"spice.choice_values.{key} lacks {sorted(missing)}")
                continue
            what = {"pin": "pin", "param": "parameter"}.get(kind)
            if what is not None:
                raise ValueError(f"spice.template: <{token}>: the part has no {what} {key!r}")
            raise ValueError(
                f"spice.template: unknown placeholder <{token}>; use <name>, <model>, "
                "<pin:P>, <param:X>, or <choice:X>"
            )
