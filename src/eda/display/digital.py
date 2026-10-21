"""Logic levels at a device's input pins during co-simulation.

The co-simulation advances in windows. For a net driven by a firmware GPIO the emulator
reports every state change with its exact time (`rp2040.Segments`); for other nets only
the voltage at the end of each window is known. A device turns both into logic levels
against its own thresholds, so a level the device cannot read (between VIL and VIH, e.g.
a 3.3 V output into a 5 V CMOS input needing 3.5 V) is caught.
"""

from dataclasses import dataclass, field

type Segments = list[tuple[float, str]]  # (time in s, GPIO state), as rp2040.Segments
type Edge = tuple[float, str, bool]  # (time in s, role, new level)

_DRIVEN_DEFAULT = {"high": 1.0, "low": 0.0}  # x vio until a window measures it


@dataclass
class Inputs:
    """Levels of a device's input roles; `nets` maps each role to its net (None: open)."""

    nets: dict[str, str | None]
    vih: float
    vil: float
    vio: float  # the MCU's I/O voltage
    open_level: bool = True  # an open input reads high (internal pull-ups)
    levels: dict[str, bool] = field(default_factory=lambda: dict[str, bool]())
    state_volts: dict[tuple[str, str], float] = field(
        default_factory=lambda: dict[tuple[str, str], float]()
    )
    undefined: dict[str, float] = field(default_factory=lambda: dict[str, float]())  # role → V

    def level(self, role: str, volts: float) -> bool:
        if volts >= self.vih:
            return True
        if volts <= self.vil:
            return False
        self.undefined.setdefault(role, volts)
        return self.levels.get(role, volts > (self.vih + self.vil) / 2)

    def window(
        self, start: float, gpio: dict[str, Segments], volts: dict[str, float]
    ) -> list[Edge]:
        """Level changes during one window, in time order (the first call reports every
        role's level at `start`). `gpio` holds the state segments of firmware-driven nets,
        `volts` every net's voltage at the end of the window."""
        edges: list[Edge] = []
        for role, net in self.nets.items():
            if net is None:
                edges += self._set(role, start, self.open_level)
                continue
            segments = gpio.get(net)
            end_volts = volts.get(net, 0.0)
            if not segments:
                edges += self._set(role, start, self.level(role, end_volts))
                continue
            if len(segments) == 1:  # one state all window: its voltage is measured
                self.state_volts[net, segments[0][1]] = end_volts
            for time, state in segments:
                guess = self.state_volts.get((net, state))
                if guess is None:
                    fraction = _DRIVEN_DEFAULT.get(state)
                    guess = fraction * self.vio if fraction is not None else end_volts
                edges += self._set(role, max(time, start), self.level(role, guess))
        edges.sort(key=lambda edge: edge[0])
        return edges

    def _set(self, role: str, time: float, level: bool) -> list[Edge]:
        if self.levels.get(role) == level:
            return []
        self.levels[role] = level
        return [(time, role, level)]
