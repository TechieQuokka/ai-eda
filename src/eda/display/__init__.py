"""Virtual displays: what a display part shows during a simulation.

Each display part (a part definition with a `display:` section) becomes a `Screen`: the
frames it showed over the run, each with its text (rows joined by newlines) and the state
needed to draw it. Segment displays are decoded from their LED currents after the
simulation (`segments`); controller-driven displays are fed by the co-simulation.
"""

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue


@dataclass(frozen=True)
class Frame:
    time: float  # s
    text: str  # rows joined by "\n"
    state: dict[str, JsonValue] = field(default_factory=lambda: dict[str, JsonValue]())


class Screen(Protocol):
    ref: str
    kind: str

    @property
    def meta(self) -> dict[str, JsonValue]:
        """What a viewer needs besides the frames to draw them (colours, sizes)."""
        ...

    def frame_at(self, time: float | None) -> Frame:
        """What the display shows at `time` (None: the end of the run)."""
        ...

    def timeline(self) -> list[Frame]:
        """The frames over the run, one per change of what is shown."""
        ...

    def svg(self, frame: Frame) -> str:
        """A drawing of `frame`."""
        ...


@dataclass
class Recorded:
    """A screen whose frames were recorded during the run (controller displays)."""

    ref: str
    kind: str
    frames: list[Frame]
    draw: "Drawer"
    meta: dict[str, JsonValue] = field(default_factory=lambda: dict[str, JsonValue]())

    def frame_at(self, time: float | None) -> Frame:
        if time is None:
            return self.frames[-1]
        shown = self.frames[0]
        for frame in self.frames:
            if frame.time > time + 1e-12:
                break
            shown = frame
        return shown

    def timeline(self) -> list[Frame]:
        return self.frames

    def svg(self, frame: Frame) -> str:
        return self.draw(frame)


class Drawer(Protocol):
    def __call__(self, frame: Frame) -> str: ...


MAX_FRAMES = 2000  # frames kept per screen for playback; the rest are thinned out


def thin(frames: list[Frame]) -> list[Frame]:
    """At most MAX_FRAMES frames, always keeping the first and the last."""
    if len(frames) <= MAX_FRAMES:
        return frames
    step = len(frames) / (MAX_FRAMES - 1)
    kept = [frames[min(len(frames) - 1, round(i * step))] for i in range(MAX_FRAMES - 1)]
    return [*kept, frames[-1]]
