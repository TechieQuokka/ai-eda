from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

IDENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

Ident = Annotated[str, StringConstraints(pattern=IDENT_PATTERN)]
Scalar = str | int | float | bool


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
