import shutil
from pathlib import Path

import pytest

from eda import cosim, pipeline
from eda.adapters import ngspice_shared, picosdk, rp2040
from eda.design import load_design
from eda.project import REPO_ROOT
from eda.result import Diagnostics
from eda.schema.part import McuSpec
from eda.verify.measure import MeasureTypeError, check, evaluate, parse_measure

EXAMPLE = REPO_ROOT / "projects" / "pico_io"


def _has_sdk() -> bool:
    try:
        picosdk.sdk_path()
    except picosdk.PicoSdkError:
        return False
    return shutil.which("arm-none-eabi-gcc") is not None and shutil.which("cmake") is not None


requires_pico_sdk = pytest.mark.skipif(not _has_sdk(), reason="needs pico-sdk and arm gcc")

BUILD_LOG = """\
[ 15%] Building C object CMakeFiles/pico_io.dir/main.c.o
/work/fw/main.c: In function 'broken':
/work/fw/main.c:25:34: error: 'y' undeclared (first use in this function)
   25 | int broken(void) { int x; return y; }
/work/fw/main.c:25:34: note: each undeclared identifier is reported only once
/work/fw/main.c:25:24: warning: unused variable 'x' [-Wunused-variable]
/work/fw/main.c:25:24: warning: unused variable 'x' [-Wunused-variable]
/usr/bin/arm-none-eabi-ld: CMakeFiles/pico_io.dir/main.c.o: in function `broken':
/work/fw/main.c:26:(.text.startup.main+0x5c): undefined reference to `undefined_fn'
CMakeFiles/pico_io.dir/util.c.o:(.text.f+0x4): undefined reference to `g'
collect2: error: ld returned 1 exit status
gmake[2]: *** [CMakeFiles/pico_io.dir/build.make:76: CMakeFiles/pico_io.dir/main.c.o] Error 1
CMake Error at CMakeLists.txt:7 (target_link_libraries):
  Target "pico_io" links to: hardware_nope

-- Configuring incomplete, errors occurred!
"""


def test_parse_build_log() -> None:
    messages = picosdk.parse(BUILD_LOG)
    summary = [(m.stage, m.kind, m.file and m.file.name, m.line, m.column) for m in messages]
    assert summary == [
        ("compile", "error", "main.c", 25, 34),
        ("compile", "warning", "main.c", 25, 24),  # duplicates are dropped
        ("link", "error", "main.c", 26, None),
        ("link", "error", "util.c.o", None, None),
        ("cmake", "error", "CMakeLists.txt", 7, None),
    ]
    assert messages[0].message == "'y' undeclared (first use in this function)"
    assert messages[2].message == "undefined reference to `undefined_fn'"
    assert messages[4].message == 'Target "pico_io" links to: hardware_nope'


def test_missing_sdk_is_a_tool_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pico_io"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("out", "iterations.jsonl"))
    monkeypatch.setenv("PICO_SDK_PATH", str(tmp_path / "no-sdk"))
    result = pipeline.fw(root, build=True)
    assert result.tool_error
    assert [e.rule for e in result.errors] == ["fw_toolchain"]


@requires_pico_sdk
def test_build_reports_errors_then_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "pico_io"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("out", "iterations.jsonl"))
    main = root / "firmware" / "main.c"
    good = main.read_text(encoding="utf-8")
    main.write_text(good + "int broken(void) { int x; return y; }\n", encoding="utf-8")
    line = good.count("\n") + 1

    result = pipeline.fw(root, build=True)
    assert not result.ok and not result.tool_error
    error = next(e for e in result.errors if e.rule == "fw_compile_error")
    assert (error.file, error.line) == ("firmware/main.c", line)
    assert "'y' undeclared" in error.message

    main.write_text(good + "int unused(void) { int x; return 0; }\n", encoding="utf-8")
    result = pipeline.fw(root, build=True)
    assert result.ok, result.errors
    assert [w.rule for w in result.warnings] == ["fw_compile_warning"]
    build = result.data["build"]
    assert isinstance(build, dict)
    assert build["elf"] == "out/fw-build/pico_io.elf"
    assert (root / "out" / "fw-build" / "pico_io.uf2").is_file()
    size = build["size"]
    assert isinstance(size, dict) and isinstance(size["flash"], int) and size["flash"] > 1000

    # The warning comes back on a rebuild even though main.c did not change.
    result = pipeline.fw(root, build=True)
    assert [w.rule for w in result.warnings] == ["fw_compile_warning"]


# --- co-simulation -----------------------------------------------------------------------


def _has_cosim() -> bool:
    try:
        rp2040.bootrom()
    except rp2040.EmulatorError:
        return False
    return (
        _has_sdk()
        and shutil.which("node") is not None
        and (rp2040.PACKAGE_DIR / "package.json").is_file()
        and ngspice_shared.library_path() is not None
    )


requires_cosim = pytest.mark.skipif(
    not _has_cosim(), reason="needs pico-sdk, rp2040js + boot ROM, and libngspice"
)


@pytest.fixture
def pico(tmp_path: Path) -> Path:
    root = tmp_path / "pico_io"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("out", "iterations.jsonl"))
    (root / "design" / "board.yaml").unlink()
    return root


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_period_measure() -> None:
    node = parse_measure("period(voltage(OUT), 1V)")
    measure = check(node, "tran")
    assert measure.unit == "s"
    xs = [i * 0.1 for i in range(41)]  # 0..4 s, square wave with a 1 s period
    ys = [2.0 if (x % 1.0) < 0.5 else 0.0 for x in xs]
    value = evaluate(node, "tran", xs, lambda _probe: ys)
    assert value == pytest.approx(1.0, abs=0.01)
    with pytest.raises(MeasureTypeError):
        check(node, "ac")


def test_thevenin_average() -> None:
    mcu = McuSpec(family="rp2040", vio=3.3, supply_pin="3V3", ground_pin="GND", gpio={})
    thevenin = cosim._Thevenin(mcu)  # pyright: ignore[reportPrivateUsage]
    # Driven high for a quarter of the window: a 25 % PWM averages to vio / 4.
    volts, ohms = thevenin.average([(0.0, "high"), (0.25e-3, "low")], 1e-3, False)
    assert volts == pytest.approx(3.3 / 4)
    assert ohms == pytest.approx(mcu.drive_ohm)
    assert thevenin.average([(0.0, "pullup")], 1e-3, False) == (3.3, mcu.pull_ohm)


def test_firmware_test_rules(pico: Path) -> None:
    tests = pico / "design" / "test.yaml"
    _edit(tests, "analysis: { type: tran, step: 1ms, stop: 2.2s }", "analysis: { type: op }")
    _edit(tests, "SW1: { state: pressed }", "SW1: { state: stuck }")
    _edit(tests, "      RV1: { position: 25% }", "      RV1: { position: 25% }\n      R9: { x: 1 }")
    text = tests.read_text(encoding="utf-8")
    text += (
        "  not_fw:\n    analysis: { type: tran, step: 1ms, stop: 10ms }\n    expect:\n"
        "      - { serial: UART_TX, match: x, min: 1 }\n"
        "  bad_net:\n    analysis: { type: tran, step: 1ms, stop: 10ms }\n"
        "    firmware: true\n    expect:\n      - { serial: LED, match: x, min: 1 }\n"
    )
    tests.write_text(text, encoding="utf-8")
    diag = Diagnostics()
    load_design(pico, diag)
    assert {e.rule for e in diag.errors} == {
        "firmware_needs_tran",  # firmware_blink switched to op ...
        "invalid_measure",  # ... where period() and at() do not apply
        "invalid_choice",
        "serial_needs_firmware",
        "serial_net",
        "unknown_part",
    }


@requires_cosim
def test_firmware_in_the_loop(pico: Path) -> None:
    result = pipeline.sim(pico)
    assert result.ok, result.errors
    tests = result.data["tests"]
    assert isinstance(tests, dict)
    blink = tests["firmware_blink"]
    assert isinstance(blink, dict)
    firmware = blink["firmware"]
    assert isinstance(firmware, dict)
    serial = firmware["serial"]
    assert isinstance(serial, dict) and "pot=2047" in str(serial["UART_TX"])

    # A firmware change shows up in the circuit: blinking at 250 ms breaks the period.
    _edit(pico / "firmware" / "main.c", "pressed ? 100 : 500", "pressed ? 100 : 250")
    result = pipeline.sim(pico, "firmware_blink")
    failed = [e for e in result.errors if e.rule == "expect"]
    assert [e.details["measure"] for e in failed] == [
        "at(current(D1), 700ms)",
        "period(current(D1), 3mA)",
    ]
    value = failed[1].details["value"]
    assert isinstance(value, float) and value == pytest.approx(0.5, abs=0.01)
