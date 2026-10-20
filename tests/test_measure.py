import math

import pytest

from eda.verify.measure import MeasureSyntaxError, parse_measure, reduce


def test_parse_forms() -> None:
    assert parse_measure("voltage(N1)").probe.targets == ("N1",)
    diff = parse_measure("voltage( A , B )")
    assert diff.probe.targets == ("A", "B")
    assert diff.text == "voltage(A, B)"
    reduced = parse_measure("max(current(R1))")
    assert (reduced.reduction, reduced.probe.kind, reduced.probe.unit) == ("max", "current", "A")


@pytest.mark.parametrize("text", ["volt(N1)", "median(voltage(N1))", "current(A, B)", "N1", ""])
def test_parse_rejects(text: str) -> None:
    with pytest.raises(MeasureSyntaxError):
        parse_measure(text)


def test_reductions() -> None:
    time = [0.0, 1.0, 2.0]
    values = [0.0, 2.0, 0.0]
    assert reduce("max", time, values) == 2.0
    assert reduce("min", time, values) == 0.0
    assert reduce("pp", time, values) == 2.0
    assert reduce("initial", time, values) == 0.0
    assert reduce("final", time, values) == 0.0
    assert reduce("avg", time, values) == 1.0  # trapezoid area 2 over 2 s
    assert math.isclose(reduce("rms", time, values), math.sqrt(2.0))
    assert reduce(None, None, [5.0]) == 5.0
