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
}

# Accepted spellings → canonical unit symbol.
UNITS: Final[dict[str, str]] = {
    "ohm": "ohm",
    "Ω": "ohm",
    "F": "F",
    "H": "H",
    "V": "V",
    "A": "A",
    "s": "s",
    "Hz": "Hz",
}

_DISPLAY: Final[dict[str, str]] = {"ohm": "Ω"}

_QUANTITY_RE: Final = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r" ?(?P<prefix>[pnuµmkMG])?"
    r"(?P<unit>ohm|Ω|Hz|F|H|V|A|s)$"
)


class QuantityError(ValueError):
    """Raised when text is not a valid quantity of the expected unit."""


def parse_quantity(text: object, unit: str) -> float:
    """Parse `text` as a quantity in `unit` (canonical symbol) and return the SI value."""
    if unit not in UNITS.values():
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
    found = UNITS[match["unit"]]
    if found != unit:
        raise QuantityError(f"wrong unit in {text!r}: expected {unit}, got {found}")
    value = float(match["number"]) * PREFIXES.get(match["prefix"] or "", 1.0)
    if not math.isfinite(value):
        raise QuantityError(f"quantity {text!r} is not finite")
    return value


def format_quantity(value: float, unit: str, digits: int = 4) -> str:
    """Format an SI value with an engineering prefix, e.g. 0.00931 A → '9.31mA'."""
    symbol = _display_unit(unit)
    if value == 0 or not math.isfinite(value):
        return f"{value:g}{symbol}"
    exponent = math.floor(math.log10(abs(value)) / 3) * 3
    exponent = max(-12, min(9, exponent))
    prefix = {-12: "p", -9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[exponent]
    scaled = value / 10**exponent
    return f"{scaled:.{digits}g}{prefix}{symbol}"


def _display_unit(unit: str) -> str:
    return _DISPLAY.get(unit, unit)
