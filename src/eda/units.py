"""Physical quantities written as text: number + optional SI prefix + mandatory unit.

Prefixes are case-sensitive (`m` = milli, `M` = mega). Values are stored as SI floats.
"""

import math
import re
from typing import Final

PREFIXES: Final[dict[str, float]] = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}

# Accepted spellings → (canonical unit symbol, factor to the canonical unit).
UNITS: Final[dict[str, tuple[str, float]]] = {
    "ohm": ("ohm", 1.0),
    "Ω": ("ohm", 1.0),
    "F": ("F", 1.0),
    "H": ("H", 1.0),
    "V": ("V", 1.0),
    "A": ("A", 1.0),
    "s": ("s", 1.0),
    "Hz": ("Hz", 1.0),
    "dB": ("dB", 1.0),
    "deg": ("deg", 1.0),
    "V/s": ("V/s", 1.0),
    "V/ms": ("V/s", 1e3),
    "V/us": ("V/s", 1e6),
    "m": ("m", 1.0),
    "%": ("%", 1.0),
    "lx": ("lx", 1.0),  # illuminance (light sensors)
}
CANONICAL: Final = frozenset(unit for unit, _ in UNITS.values())

# Logarithmic and compound units take no SI prefix.
_NO_PREFIX: Final = frozenset({"dB", "deg", "V/s", "V/ms", "V/us", "%"})
_DISPLAY: Final[dict[str, str]] = {"ohm": "Ω"}

_QUANTITY_RE: Final = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r" ?(?P<prefix>[pnuµmkMGT])?"
    r"(?P<unit>V/us|V/ms|V/s|ohm|Ω|Hz|dB|deg|lx|F|H|V|A|s|m|%)$"
)


class QuantityError(ValueError):
    """Raised when text is not a valid quantity of the expected unit."""


def parse_quantity(text: object, unit: str) -> float:
    """Parse `text` as a quantity in `unit` (canonical symbol) and return the SI value."""
    if unit not in CANONICAL:
        raise ValueError(f"unknown unit {unit!r}")
    example = f"10{_display_unit(unit)}"
    if isinstance(text, bool) or not isinstance(text, (str, int, float)):
        raise QuantityError(f"expected a quantity in {unit} such as {example!r}")
    if not isinstance(text, str):
        raise QuantityError(f"unit required: write {text}{_display_unit(unit)!s}, not {text}")
    match = _QUANTITY_RE.match(text.strip())
    if match is None:
        raise QuantityError(
            f"invalid quantity {text!r}: expected number + optional prefix "
            f"(p n u m k M G) + unit {unit!r}, e.g. {example!r}"
        )
    found, factor = UNITS[match["unit"]]
    if found != unit:
        raise QuantityError(f"wrong unit in {text!r}: expected {unit}, got {found}")
    if match["prefix"] and match["unit"] in _NO_PREFIX:
        raise QuantityError(f"{match['unit']} takes no prefix: {text!r}")
    value = float(match["number"]) * PREFIXES.get(match["prefix"] or "", 1.0) * factor
    if not math.isfinite(value):
        raise QuantityError(f"quantity {text!r} is not finite")
    return value


def parse_any_quantity(text: str) -> tuple[float, str]:
    """Parse a quantity of any known unit; returns (SI value, canonical unit)."""
    match = _QUANTITY_RE.match(text.strip())
    if match is None:
        raise QuantityError(
            f"invalid quantity {text!r}: expected number + optional prefix + unit, e.g. '4.7kohm'"
        )
    unit = UNITS[match["unit"]][0]
    return parse_quantity(text, unit), unit


def format_quantity(value: float, unit: str, digits: int = 4) -> str:
    """Format an SI value with an engineering prefix, e.g. 0.00931 A → '9.31mA'."""
    symbol = _display_unit(unit)
    if unit in ("dB", "deg", "%"):
        return f"{value:.{digits}g}{symbol}"
    if unit == "V/s":
        return f"{value / 1e6:.{digits}g}V/us"
    if abs(value) < 1e-15:  # below anything a simulation resolves: show as zero
        return f"0{symbol}"
    if not math.isfinite(value):
        return f"{value:g}{symbol}"
    exponent = math.floor(math.log10(abs(value)) / 3) * 3
    exponent = max(-12, min(12, exponent))
    prefix = {-12: "p", -9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M", 9: "G", 12: "T"}[exponent]
    scaled = value / 10**exponent
    if abs(scaled) >= 1000:  # beyond T: an exponent reads better than '1e+18T'
        return f"{value:.{digits}g}{symbol}"
    return f"{scaled:.{digits}g}{prefix}{symbol}"


def _display_unit(unit: str) -> str:
    return _DISPLAY.get(unit, unit)
