from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from eda.schema.common import Ident, StrictModel

PinRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*\.[^.\s]+$")]


class PartInstance(StrictModel):
    """One entry under `parts:`. Keys other than `type` are part parameters."""

    model_config = ConfigDict(extra="allow")

    type: Ident

    @property
    def params(self) -> dict[str, object]:
        return dict(self.model_extra or {})


class Circuit(StrictModel):
    """`design/circuit.yaml`."""

    description: str = ""
    parts: dict[Ident, PartInstance]
    nets: dict[Ident, Annotated[list[PinRef], Field(min_length=1)]]
