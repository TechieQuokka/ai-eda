from eda.schema.common import Ident, StrictModel


class Project(StrictModel):
    """`project.yaml`."""

    name: Ident
    description: str = ""
