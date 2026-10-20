from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from eda.schema.common import Ident, StrictModel

PinRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*\.[^.\s]+$")]


class PartInstance(StrictModel):
    """One entry under `parts:`. Keys other than `type`, `footprint`, and `pads` are part
    parameters. `footprint` / `pads` override the part's default PCB footprint."""

    model_config = ConfigDict(extra="allow")

    type: Ident
    footprint: str | None = None
    pads: dict[str, str | int | list[str]] | None = None

    @property
    def params(self) -> dict[str, object]:
        return dict(self.model_extra or {})


class Circuit(StrictModel):
    """`design/circuit.yaml`."""

    description: str = ""
    parts: dict[Ident, PartInstance]
    nets: dict[Ident, Annotated[list[PinRef], Field(min_length=1)]]
