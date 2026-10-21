import cmath
import math

import pytest

from eda.verify.measure import (
    Call,
    MeasureSyntaxError,
    MeasureTypeError,
    Probe,
    check,
    evaluate,
    parse_measure,
    reduce,
)


def test_parse_forms() -> None:
    assert parse_measure("voltage(N1)") == Probe("voltage", ("N1",))
    diff = parse_measure("voltage( A , B )")
    assert isinstance(diff, Probe)
    assert diff.text == "voltage(A, B)"
    reduced = parse_measure("max(current(R1))")
    assert reduced == Call("max", Probe("current", ("R1",)))
    pin = parse_measure("current(U1.in+)")
    assert pin == Probe("current", ("U1",), "in+")
    assert pin.text == "current(U1.in+)"
    nested = parse_measure("at(db(voltage(OUT)), 1kHz)")
    assert isinstance(nested, Call)
    assert nested.text == "at(db(voltage(OUT)), 1kHz)"
    assert nested.quantity is not None and nested.quantity.value == 1000.0


@pytest.mark.parametrize(
    "text",
    [
        "volt(N1)",
        "median(voltage(N1))",
        "current(A, B)",
        "N1",
        "",
        "at(voltage(N1))",
        "max(voltage(N1), 1V)",
        "at(voltage(N1), 1meg)",
        "max(voltage(N1)",
    ],
)
def test_parse_rejects(text: str) -> None:
    with pytest.raises(MeasureSyntaxError):
        parse_measure(text)


@pytest.mark.parametrize(
    ("text", "domain", "unit"),
    [
        ("voltage(N1)", "op", "V"),
        ("max(current(R1))", "tran", "A"),
        ("at(voltage(OUT), 1ms)", "tran", "V"),
        ("cross(voltage(OUT), 2.5V)", "tran", "s"),
        ("at(db(voltage(OUT)), 1kHz)", "ac", "dB"),
        ("max(phase(voltage(OUT)))", "ac", "deg"),
        ("cross(db(voltage(OUT)), -3dB)", "ac", "Hz"),
        ("at(mag(current(R1)), 1kHz)", "ac", "A"),
    ],
)
def test_check_units(text: str, domain: str, unit: str) -> None:
    assert check(parse_measure(text), domain).unit == unit  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("text", "domain"),
    [
        ("voltage(N1)", "tran"),
        ("voltage(N1)", "ac"),
        ("max(voltage(N1))", "ac"),
        ("db(voltage(N1))", "tran"),
        ("avg(db(voltage(N1)))", "ac"),
        ("at(voltage(N1), 1kHz)", "tran"),
        ("cross(db(voltage(N1)), 1V)", "ac"),
        ("at(voltage(N1), 1ms)", "op"),
    ],
)
def test_check_rejects(text: str, domain: str) -> None:
    with pytest.raises(MeasureTypeError):
        check(parse_measure(text), domain)  # pyright: ignore[reportArgumentType]


def test_evaluate_tran() -> None:
    time = [0.0, 1.0, 2.0, 3.0]
    wave = [0.0, 2.0, 4.0, 4.0]

    def run(text: str) -> float:
        node = check(parse_measure(text), "tran").node
        return evaluate(node, "tran", time, lambda _: wave)

    assert run("max(voltage(A))") == 4.0
    assert run("at(voltage(A), 1.5s)") == 3.0
    assert run("cross(voltage(A), 1V)") == 0.5
    assert math.isnan(run("cross(voltage(A), 9V)"))


def test_evaluate_ac_lowpass() -> None:
    freq = [10.0**k for k in range(0, 7)]
    fc = 1000.0
    response = [1 / (1 + 1j * f / fc) for f in freq]

    def run(text: str) -> float:
        node = check(parse_measure(text), "ac").node
        return evaluate(node, "ac", freq, lambda _: response)

    assert run("at(db(voltage(OUT)), 1Hz)") == pytest.approx(0.0, abs=1e-5)
    assert run("at(db(voltage(OUT)), 1kHz)") == pytest.approx(-3.0103, abs=1e-3)
    assert run("at(phase(voltage(OUT)), 1kHz)") == pytest.approx(-45.0, abs=1e-6)
    # Log-x interpolation between 100 Hz and 1 kHz grid points: close to the true corner.
    assert run("cross(db(voltage(OUT)), -3dB)") == pytest.approx(fc, rel=0.1)
    assert run("min(phase(voltage(OUT)))") == pytest.approx(
        math.degrees(cmath.phase(response[-1])), abs=1e-6
    )


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


def test_pulse_measures() -> None:
    # 1 s period, high for 0.3 s from 0.2 s of each second; the run starts and ends mid-gap
    # and the first pulse is complete, so only whole pulses and gaps count.
    xs = [i * 0.001 for i in range(4651)]  # 0 .. 4.65 s
    ys = [2.0 if 0.2 <= x % 1.0 < 0.5 else 0.0 for x in xs]

    def value(text: str) -> float:
        node = parse_measure(text)
        check(node, "tran")
        return evaluate(node, "tran", xs, lambda _probe: ys)

    assert value("period(voltage(A), 1V)") == pytest.approx(1.0, abs=2e-3)
    assert value("width(voltage(A), 1V)") == pytest.approx(0.3, abs=2e-3)
    assert value("gap(voltage(A), 1V)") == pytest.approx(0.7, abs=2e-3)
    assert value("duty(voltage(A), 1V)") == pytest.approx(30.0, abs=0.3)
    assert check(parse_measure("duty(voltage(A), 1V)"), "tran").unit == "%"
    assert check(parse_measure("width(current(D1), 3mA)"), "tran").unit == "s"
    flat = [0.0] * len(xs)
    node = parse_measure("width(voltage(A), 1V)")
    assert math.isnan(evaluate(node, "tran", xs, lambda _probe: flat))


@pytest.mark.parametrize(
    ("text", "domain", "message"),
    [
        ("width(db(voltage(A)), 1dB)", "ac", "needs a tran analysis"),
        ("gap(voltage(A), 1V)", "op", "needs a tran or ac analysis"),
        ("duty(voltage(A), 1mA)", "tran", "must be in V"),
    ],
)
def test_pulse_measure_rules(text: str, domain: str, message: str) -> None:
    with pytest.raises(MeasureTypeError, match=message):
        check(parse_measure(text), domain)  # pyright: ignore[reportArgumentType]
