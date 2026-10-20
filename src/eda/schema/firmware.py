from typing import Literal, Self

from pydantic import Field, model_validator

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
    sources: list[str] = Field(default_factory=lambda: ["firmware"])  # dirs to scan
    signals: dict[Ident, SignalSpec]  # net name → use; macros become PIN_<NET>
