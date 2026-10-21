from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from eda.schema.common import Ident, StrictModel


class SignalSpec(StrictModel):
    """How firmware uses the GPIO on one net: a plain `mode`, or a peripheral `function`."""

    mode: Literal["input", "output"] | None = None
    function: str | None = None  # e.g. UART0_TX, ADC0, PWM7_B, I2C0_SDA
    pull: Literal["up", "down", "none"] = "none"
    initial: Literal["high", "low"] = "low"  # outputs only
    description: str = ""

    @model_validator(mode="after")
    def _mode_xor_function(self) -> Self:
        if (self.mode is None) == (self.function is None):
            raise ValueError("give exactly one of `mode` (input/output) or `function`")
        return self


class FirmwareFile(StrictModel):
    """`design/firmware.yaml`: the MCU and the role of every GPIO net."""

    mcu: Ident  # part reference of the microcontroller in circuit.yaml
    # Firmware directories, relative to the project; pins.h goes into the first one.
    sources: list[str] = Field(default_factory=lambda: ["firmware"], min_length=1)
    signals: dict[Ident, SignalSpec]  # net name → use; macros become PIN_<NET>

    @field_validator("sources")
    @classmethod
    def _inside_project(cls, sources: list[str]) -> list[str]:
        for source in sources:
            path = PurePosixPath(source)
            if not source or path.is_absolute() or ".." in path.parts or "\\" in source:
                raise ValueError(
                    f"{source!r}: sources are directories inside the project, e.g. firmware"
                )
        return sources
