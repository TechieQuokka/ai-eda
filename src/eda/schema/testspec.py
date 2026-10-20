from typing import Annotated, Literal

from pydantic import Field

from eda.schema.common import Ident, StrictModel


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


Analysis = Annotated[OpAnalysis | TranAnalysis | AcAnalysis, Field(discriminator="type")]


class Expectation(StrictModel):
    measure: str
    min: str | None = None
    max: str | None = None


class SimTest(StrictModel):
    description: str = ""
    analysis: Analysis
    expect: Annotated[list[Expectation], Field(min_length=1)]


class SimTestFile(StrictModel):
    """`design/test.yaml`."""

    tests: dict[Ident, SimTest]
