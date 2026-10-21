from typing import Annotated

from pydantic import Field, StringConstraints

from eda.schema.common import Ident, Scalar, StrictModel

# A key is one character (written as itself in a key string) or a word of two or more
# letters, digits, and underscores (written as {word}, e.g. {enter}).
KeyName = Annotated[str, StringConstraints(pattern=r"^(?:[^{}]|[A-Za-z0-9_]{2,})$")]


class KeyAction(StrictModel):
    """A key that is not a plain push button: the part, and the parameters pressing and
    releasing the key set on it (e.g. a pot turned to a position and back)."""

    part: Ident
    press: Annotated[dict[str, Scalar], Field(min_length=1)]
    release: Annotated[dict[str, Scalar], Field(min_length=1)]


class InputsFile(StrictModel):
    """`design/inputs.yaml`: how a person operates the board. `keys` maps each key to the
    part it presses; a bare part name is a button (state: pressed, then state: open).
    Key strings in tests (`{ at: 150ms, keys: "12+34=" }`) and in `eda try --keys` press
    one key after another: each held for `hold`, then `gap` until the next."""

    keys: dict[KeyName, Ident | KeyAction] = Field(default_factory=dict[str, Ident | KeyAction])
    hold: str = "40ms"  # how long a key is held down
    gap: str = "40ms"  # pause after releasing a key before the next one
    start: str = "100ms"  # eda try: time of the first input (after power-up)
    settle: str = "100ms"  # eda try: the run goes on this long after the last input
    step: str = "1ms"  # eda try: time step of the run
