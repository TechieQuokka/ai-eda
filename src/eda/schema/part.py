from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from eda.schema.common import Ident, StrictModel
from eda.units import CANONICAL, parse_quantity


class ParamSpec(StrictModel):
    """A part parameter: either a quantity with `unit` or one of `choices`."""

    description: str = ""
    unit: str | None = None
    choices: list[str] | None = None
    default: str | None = None

    @model_validator(mode="after")
    def _unit_xor_choices(self) -> Self:
        if (self.unit is None) == (self.choices is None):
            raise ValueError("exactly one of 'unit' or 'choices' is required")
        if self.unit is not None and self.unit not in CANONICAL:
            raise ValueError(f"unknown unit {self.unit!r}")
        if (
            self.choices is not None
            and self.default is not None
            and (self.default not in self.choices)
        ):
            raise ValueError(f"default {self.default!r} is not one of the choices")
        return self


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
    generator: Literal["mcu"] | None = None  # netlist written by code, not by `template`
    # `<choice:P>` in the template becomes choice_values[P][value of P], e.g. a switch's
    # resistance for state open / pressed.
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
    optional_pins: list[str] = Field(default_factory=list[str])  # may stay unconnected
    dc_path: bool = True
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
        if self.pcb is not None and set(self.pcb.pads) != set(self.pins):
            raise ValueError("pcb.pads must map every pin exactly once")
        if self.spice.model_param is not None:
            spec = self.params.get(self.spice.model_param)
            if spec is None or spec.choices is None:
                raise ValueError("spice.model_param must name a choices parameter")
            missing = set(spec.choices) - set(self.spice.models)
            if missing:
                raise ValueError(f"spice.models lacks entries for {sorted(missing)}")
        return self
