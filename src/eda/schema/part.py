from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from eda.schema.common import Ident, StrictModel
from eda.units import UNITS


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
        if self.unit is not None and self.unit not in UNITS.values():
            raise ValueError(f"unknown unit {self.unit!r}")
        if (
            self.choices is not None
            and self.default is not None
            and (self.default not in self.choices)
        ):
            raise ValueError(f"default {self.default!r} is not one of the choices")
        return self


class SpiceSpec(StrictModel):
    """How a part becomes SPICE.

    `template` placeholders: `<name>` element name, `<pin:P>` node of pin P,
    `<param:X>` parameter value, `<model>` model name.
    `models` maps a value of `model_param` to a `.model` body such as `D(IS=1e-14)`.
    `current` is the raw-file probe for the part's current, using `<name>`.
    """

    prefix: Annotated[str, StringConstraints(pattern=r"^[A-Z]$")]
    template: str
    current: str
    model_param: str | None = None
    models: dict[str, str] = Field(default_factory=dict[str, str])


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
    dc_path: bool = True
    datasheet: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.pins)) != len(self.pins):
            raise ValueError("pin names must be unique")
        if set(self.symbol.ports) != set(self.pins):
            raise ValueError("symbol.ports must map every pin exactly once")
        if self.spice.model_param is not None:
            spec = self.params.get(self.spice.model_param)
            if spec is None or spec.choices is None:
                raise ValueError("spice.model_param must name a choices parameter")
            missing = set(spec.choices) - set(self.spice.models)
            if missing:
                raise ValueError(f"spice.models lacks entries for {sorted(missing)}")
        return self
