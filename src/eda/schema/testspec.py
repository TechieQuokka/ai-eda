from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from eda.schema.common import Ident, Scalar, StrictModel


class OpAnalysis(StrictModel):
    type: Literal["op"]


class TranAnalysis(StrictModel):
    type: Literal["tran"]
    step: str
    stop: str
    start: str | None = None


class AcAnalysis(StrictModel):
    type: Literal["ac"]
    start: str
    stop: str
    points_per_decade: Annotated[int, Field(ge=1, le=1000)] = 20


class DcAnalysis(StrictModel):
    """Operating points while one part parameter steps from `start` to `stop`."""

    type: Literal["dc"]
    sweep: str  # PART.param, e.g. V1.voltage or LDR1.light
    start: str
    stop: str
    step: str


Analysis = Annotated[
    OpAnalysis | TranAnalysis | AcAnalysis | DcAnalysis, Field(discriminator="type")
]


class Expectation(StrictModel):
    """A `measure` of the simulated circuit, or a number read from a firmware `serial` port.

    Serial: `match` is a regular expression applied to everything the firmware sent on the
    UART TX net; with a capture group the value is the number in the last match, without
    one it is the number of matches.
    """

    measure: str | None = None
    serial: Ident | None = None  # UART TX net name
    match: str | None = None
    min: str | float | None = None
    max: str | float | None = None
    # tran only: evaluate the measure over this part of the run, e.g. from: 1s
    start: str | None = Field(default=None, alias="from")
    end: str | None = Field(default=None, alias="to")

    @model_validator(mode="after")
    def _measure_xor_serial(self) -> Self:
        if (self.measure is None) == (self.serial is None):
            raise ValueError("give exactly one of `measure` or `serial`")
        if (self.serial is None) != (self.match is None):
            raise ValueError("`serial` and `match` go together")
        return self


class SimEvent(StrictModel):
    """Something that happens during a tran test, at time `at`: a part parameter changes
    (`part` + `set`, e.g. a button is pressed) or text arrives on a firmware UART RX net
    (`serial` + `send`)."""

    at: str
    part: Ident | None = None
    set: dict[str, Scalar] | None = None
    serial: Ident | None = None  # UART RX net name
    send: str | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        part = self.part is not None or self.set is not None
        serial = self.serial is not None or self.send is not None
        if part == serial:
            raise ValueError("give either `part` + `set` or `serial` + `send`")
        if part and (self.part is None or not self.set):
            raise ValueError("`part` and `set` go together")
        if serial and (self.serial is None or self.send is None):
            raise ValueError("`serial` and `send` go together")
        return self


class SimTest(StrictModel):
    description: str = ""
    analysis: Analysis
    # Override firmware pin levels for this test, e.g. { LED: high } (net name → level).
    pins: dict[Ident, Literal["high", "low"]] = Field(
        default_factory=dict[Ident, Literal["high", "low"]]
    )
    # Part parameters for this test only, e.g. { SW1: { state: pressed } }.
    parts: dict[Ident, dict[str, Scalar]] = Field(default_factory=dict[Ident, dict[str, Scalar]])
    # Run the compiled firmware in the RP2040 emulator, co-simulated with the circuit (tran).
    firmware: bool = False
    events: list[SimEvent] = Field(default_factory=list[SimEvent])  # tran only
    expect: Annotated[list[Expectation], Field(min_length=1)]


class SimTestFile(StrictModel):
    """`design/test.yaml`."""

    tests: dict[Ident, SimTest]
