import math

import pytest

from eda.units import QuantityError, format_quantity, parse_quantity


@pytest.mark.parametrize(
    ("text", "unit", "expected"),
    [
        ("330ohm", "ohm", 330.0),
        ("4.7kohm", "ohm", 4700.0),
        ("1Mohm", "ohm", 1e6),
        ("1mohm", "ohm", 1e-3),
        ("2.2kΩ", "ohm", 2200.0),
        ("100nF", "F", 100e-9),
        ("10uF", "F", 10e-6),
        ("10µF", "F", 10e-6),
        ("10mA", "A", 0.01),
        ("-5V", "V", -5.0),
        ("1e3Hz", "Hz", 1000.0),
        ("5ms", "s", 0.005),
        ("1 kohm", "ohm", 1000.0),
    ],
)
def test_parse_valid(text: str, unit: str, expected: float) -> None:
    assert math.isclose(parse_quantity(text, unit), expected)


@pytest.mark.parametrize(
    ("value", "unit", "fragment"),
    [
        (330, "ohm", "unit required"),
        ("330", "ohm", "invalid quantity"),
        ("10V", "F", "wrong unit"),
        ("1Meg", "ohm", "invalid quantity"),
        ("abc", "V", "invalid quantity"),
        (True, "V", "expected a quantity"),
        (None, "V", "expected a quantity"),
    ],
)
def test_parse_invalid(value: object, unit: str, fragment: str) -> None:
    with pytest.raises(QuantityError, match=fragment):
        parse_quantity(value, unit)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (0.00931, "A", "9.31mA"),
        (330.0, "ohm", "330Ω"),
        (4700.0, "ohm", "4.7kΩ"),
        (0.0, "V", "0V"),
        (-0.0143, "A", "-14.3mA"),
        (1e-7, "F", "100nF"),
    ],
)
def test_format(value: float, unit: str, expected: str) -> None:
    assert format_quantity(value, unit) == expected
