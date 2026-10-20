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


Analysis = Annotated[OpAnalysis | TranAnalysis, Field(discriminator="type")]


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
