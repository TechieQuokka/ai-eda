import shutil
from pathlib import Path

import pytest

from eda import pipeline
from eda.adapters import picosdk
from eda.project import REPO_ROOT

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
