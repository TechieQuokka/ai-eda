"""Seven-segment LED displays, decoded from the current through every segment LED.

The eye averages a multiplexed display: a segment counts as lit when its mean current over
the last `persistence` (20 ms by default) reaches `lit` (100 uA: visible at all) and `ghost`
(30 %) of the brightest segment's mean, so faint ghosting reads as dark and a window that
happens to catch one multiplexing slot fewer does not blank a digit. In an operating point
the current itself is used. The lit segments of each digit are read as a character.
"""

from bisect import bisect_left
from collections.abc import Iterable
from html import escape

from pydantic import JsonValue

from eda.display import Frame
from eda.model import ResolvedPart
from eda.netlist.spice import segment_vector
from eda.schema.part import SegmentDisplaySpec
from eda.units import parse_quantity

# Lit segments (a-g) → character, in the usual seven-segment spellings.
_SHAPES = {
    "": " ",
    "abcdef": "0",
    "bc": "1",
    "abdeg": "2",
    "abcdg": "3",
    "bcfg": "4",
    "acdfg": "5",
    "acdefg": "6",
    "abc": "7",
    "abcf": "7",
    "abcdefg": "8",
    "abcdfg": "9",
    "abcfg": "9",
    "abcefg": "A",
    "cdefg": "b",
    "adef": "C",
    "deg": "c",
    "bcdeg": "d",
    "adefg": "E",
    "aefg": "F",
    "acdef": "G",
    "bcefg": "H",
    "cefg": "h",
    "bcde": "J",
    "def": "L",
    "ceg": "n",
    "cdeg": "o",
    "abefg": "P",
    "eg": "r",
    "defg": "t",
    "bcdef": "U",
    "cde": "u",
    "bcdfg": "y",
    "g": "-",
    "d": "_",
    "a": "‾",
    "abfg": "°",
}
CHARACTERS = {char: shape for shape, char in reversed(_SHAPES.items())}


def character(lit: Iterable[str]) -> str:
    """The character lit segments (names a-g, in any order; dp ignored) show; `?` if none."""
    return _SHAPES.get("".join(sorted(s for s in lit if s != "dp")), "?")


class SegmentScreen:
    kind = "segments"

    def __init__(
        self,
        part: ResolvedPart,
        x: list[float] | None,
        vectors: dict[str, list[float]],
        stop: float | None = None,
    ) -> None:
        spec = part.spec.display
        assert isinstance(spec, SegmentDisplaySpec)
        self.ref = part.ref
        self.spec = spec
        self.color = str(part.values.get("color", "red"))
        self.lit = parse_quantity(spec.lit, "A")
        self.ghost = parse_quantity(spec.ghost, "%") / 100
        self.persistence = parse_quantity(spec.persistence, "s")
        self.x = x
        self.stop = stop if stop is not None else (x[-1] if x else 0.0)
        self.currents: dict[tuple[int, str], list[float]] = {}
        for digit in range(1, len(spec.digits) + 1):
            for segment in spec.segments:
                found = vectors.get(segment_vector(part, digit, segment))  # None: pin open
                self.currents[digit, segment] = found or [0.0] * len(x or [0.0])
        # running integrals of each LED current over time, for means over any window
        self.charge: dict[tuple[int, str], list[float]] = {}
        if x is not None:
            for key, values in self.currents.items():
                total = [0.0]
                for i in range(1, len(x)):
                    total.append(total[-1] + (values[i] + values[i - 1]) / 2 * (x[i] - x[i - 1]))
                self.charge[key] = total

    @property
    def meta(self) -> dict[str, JsonValue]:
        return {
            "color": self.color,
            "segments": list[JsonValue](self.spec.segments),
            "lit": self.lit,
        }

    def _integral(self, key: tuple[int, str], time: float) -> float:
        x = self.x
        assert x is not None
        total = self.charge[key]
        if time <= x[0]:
            return 0.0
        if time >= x[-1]:
            return total[-1]
        i = bisect_left(x, time)
        span = x[i] - x[i - 1]
        f = (time - x[i - 1]) / span if span > 0 else 1.0
        values = self.currents[key]
        # trapezoid from x[i-1] to `time`, current interpolated linearly
        mid = values[i - 1] + f * (values[i] - values[i - 1])
        return total[i - 1] + (values[i - 1] + mid) / 2 * (time - x[i - 1])

    def means(self, time: float | None) -> dict[tuple[int, str], float]:
        """Mean current of each LED over the persistence window ending at `time` (A)."""
        if self.x is None:
            return {key: values[-1] for key, values in self.currents.items()}
        end = self.stop if time is None else min(time, self.x[-1])
        start = max(self.x[0], end - self.persistence)
        if end <= start:
            return {key: self.currents[key][bisect_left(self.x, end)] for key in self.currents}
        return {
            key: (self._integral(key, end) - self._integral(key, start)) / (end - start)
            for key in self.currents
        }

    def frame_at(self, time: float | None) -> Frame:
        means = self.means(time)
        threshold = max(self.lit, self.ghost * max(means.values(), default=0.0))
        digits: list[JsonValue] = []
        text = ""
        for digit in range(1, len(self.spec.digits) + 1):
            lit = [s for s in self.spec.segments if means[digit, s] >= threshold]
            text += character(lit) + ("." if "dp" in lit else "")
            digits.append(
                {
                    "lit": list[JsonValue](lit),
                    "mean": {s: float(f"{means[digit, s]:.4g}") for s in self.spec.segments},
                }
            )
        when = (self.stop if time is None else time) if self.x is not None else 0.0
        return Frame(time=when, text=text, state={"digits": digits})

    def timeline(self) -> list[Frame]:
        if self.x is None:
            return [self.frame_at(None)]
        step = self.persistence / 2
        times: list[float] = []
        t = min(self.persistence, self.stop)
        while t < self.stop - 1e-12:
            times.append(t)
            t += step
        times.append(self.stop)
        frames: list[Frame] = []
        for time in times:
            frame = self.frame_at(time)
            if not frames or _lit(frames[-1]) != _lit(frame):
                frames.append(frame)
        if frames[-1].time != self.stop:
            frames.append(self.frame_at(None))
        return frames

    def svg(self, frame: Frame) -> str:
        return draw(frame, self.spec, self.color, self.lit)


def _lit(frame: Frame) -> JsonValue:
    digits = frame.state.get("digits")
    assert isinstance(digits, list)
    return [d["lit"] for d in digits if isinstance(d, dict)]


# --- drawing ------------------------------------------------------------------------------

_COLORS = {"red": "#ff2a1a", "yellow": "#ffc21a", "green": "#38e04a"}
_DIGIT_W, _DIGIT_H, _GAP = 48.0, 84.0, 14.0
# Segment outlines in a 48 x 84 digit cell (slanted like a real display).
_POLYGONS = {
    "a": "12,4 40,4 35,10 17,10",
    "b": "41,6 37,40 32,36 35,11",
    "c": "36,44 32,78 27,73 30,48",
    "d": "8,80 30,80 25,74 12,74",
    "e": "11,44 15,48 12,73 7,78",
    "f": "16,6 19,11 16,36 11,40",
    "g": "13,42 18,39 31,39 35,42 30,45 17,45",
}


def draw(frame: Frame, spec: SegmentDisplaySpec, color: str, lit_current: float) -> str:
    on = _COLORS.get(color, "#ff2a1a")
    digits = frame.state.get("digits")
    assert isinstance(digits, list)
    width = len(digits) * (_DIGIT_W + _GAP) + _GAP
    height = _DIGIT_H + 2 * _GAP
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}" height="{height:g}" '
        f'viewBox="0 0 {width:g} {height:g}">',
        f'<rect width="{width:g}" height="{height:g}" rx="6" fill="#1b1b1b"/>',
    ]
    for index, digit in enumerate(digits):
        assert isinstance(digit, dict)
        lit = digit.get("lit")
        means = digit.get("mean")
        assert isinstance(lit, list) and isinstance(means, dict)
        dx = _GAP + index * (_DIGIT_W + _GAP)
        parts.append(f'<g transform="translate({dx:g},{_GAP:g})">')
        for segment in spec.segments:
            mean = means.get(segment)
            level = float(mean) if isinstance(mean, (int, float)) else 0.0
            if segment in lit:
                opacity = 0.35 + 0.65 * min(1.0, level / (30 * lit_current))
                fill = f'fill="{on}" fill-opacity="{opacity:.2f}"'
            else:
                fill = 'fill="#3a3a3a"'
            if segment == "dp":
                parts.append(f'<circle cx="43" cy="77" r="3.5" {fill}/>')
            else:
                parts.append(f'<polygon points="{_POLYGONS[segment]}" {fill}/>')
        parts.append("</g>")
    parts.append(f"<title>{escape(frame.text)}</title></svg>")
    return "".join(parts)
