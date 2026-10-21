"""IEC 60063 preferred-number series (E6 … E192) for picking real component values."""

import math
from dataclasses import dataclass
from typing import Final

# E3 to E24 use historical values that do not follow the formula.
_FIXED: Final[dict[str, tuple[float, ...]]] = {
    "E3": (1.0, 2.2, 4.7),
    "E6": (1.0, 1.5, 2.2, 3.3, 4.7, 6.8),
    "E12": (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2),
    "E24": (
        1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
        3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1,
    ),
}  # fmt: skip

SERIES: Final = ("E3", "E6", "E12", "E24", "E48", "E96", "E192")


def mantissas(series: str) -> tuple[float, ...]:
    if series in _FIXED:
        return _FIXED[series]
    if series not in SERIES:
        raise ValueError(f"unknown series {series!r}; use one of {', '.join(SERIES)}")
    n = int(series[1:])
    values = [round(10 ** (i / n), 2) for i in range(n)]
    if series == "E192":
        values[values.index(9.19)] = 9.20  # the one exception in the standard table
    return tuple(values)


@dataclass(frozen=True)
class Pick:
    below: float
    above: float
    nearest: float
    error: float  # relative error of `nearest` versus the target


def nearest(target: float, series: str) -> Pick:
    """Closest standard values at or below / at or above `target` (positive values only)."""
    if not target > 0 or not math.isfinite(target):
        raise ValueError("target must be a positive finite number")
    if not 1e-15 <= target <= 1e15:
        raise ValueError("target is outside 1e-15..1e15, where no standard parts exist")
    decade = 10 ** math.floor(math.log10(target))
    candidates = sorted(  # 12 significant digits: drops float noise at any magnitude
        {float(f"{m * decade * scale:.12g}") for m in mantissas(series) for scale in (0.1, 1, 10)}
    )
    below = max(c for c in candidates if c <= target * (1 + 1e-12))
    above = min(c for c in candidates if c >= target * (1 - 1e-12))
    best = min((below, above), key=lambda c: abs(math.log(c / target)))
    return Pick(below=below, above=above, nearest=best, error=(best - target) / target)
