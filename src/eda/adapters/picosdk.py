"""Thin wrapper around the Raspberry Pi pico-sdk build: CMake, arm-none-eabi-gcc, size."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from eda.project import REPO_ROOT

TIMEOUT_S = 600
LOCAL_SDK = REPO_ROOT / "tools" / "pico-sdk"
_SOURCE_SUFFIXES = (".c", ".cpp", ".cc", ".S", ".s")
# gcc: "<file>:<line>:<col>: error: <message>"
_COMPILER = re.compile(
    r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<kind>fatal error|error|warning): (?P<msg>.*)$"
)
# ld: "<file>:<line>:(<section>+0x..): undefined reference to `x'"
_LINKER = re.compile(r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):\(\S+\): (?P<msg>.*)$")
# ld without debug info: "<object>:(<section>+0x..): undefined reference to `x'"
_LINKER_NOLINE = re.compile(r"^(?P<file>[^:\s][^:]*\.o):\(\S+\): (?P<msg>.*)$")
_CMAKE = re.compile(r"^CMake (?P<kind>Error|Warning)(?: \(dev\))? at (?P<file>[^:]+):(?P<line>\d+)")


class PicoSdkError(RuntimeError):
    """The SDK or the toolchain is missing, or a build step could not run at all."""


@dataclass(frozen=True)
class Message:
    kind: str  # error | warning
    stage: str  # compile | link | cmake | build
    message: str
    file: Path | None = None
    line: int | None = None
    column: int | None = None


@dataclass
class BuildOutput:
    ok: bool
    log: str
    messages: list[Message] = field(default_factory=lambda: list[Message]())
    elf: Path | None = None


def sdk_path() -> Path:
    """`PICO_SDK_PATH` if set, else the local checkout in tools/pico-sdk."""
    env = os.environ.get("PICO_SDK_PATH")
    sdk = Path(env) if env else LOCAL_SDK
    if not (sdk / "pico_sdk_init.cmake").is_file():
        raise PicoSdkError(
            f"pico-sdk not found at {sdk} (run tools/fetch-pico-sdk.sh or set PICO_SDK_PATH)"
        )
    return sdk


def sdk_version(sdk: Path) -> str | None:
    version = sdk / "pico_sdk_version.cmake"
    if not version.is_file():
        return None
    parts: dict[str, str] = {}
    pattern = r"set\(PICO_SDK_VERSION_(MAJOR|MINOR|REVISION) (\d+)\)"
    for match in re.finditer(pattern, version.read_text(encoding="utf-8")):
        parts[match.group(1)] = match.group(2)
    if len(parts) != 3:
        return None
    return f"{parts['MAJOR']}.{parts['MINOR']}.{parts['REVISION']}"


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        hint = "sudo apt install gcc-arm-none-eabi libnewlib-arm-none-eabi"
        if name == "cmake":
            hint = "sudo apt install cmake"
        raise PicoSdkError(f"{name} not found on PATH ({hint})")
    return found


def build(source: Path, build_dir: Path, target: str, board: str = "pico") -> BuildOutput:
    """Configure (first time) and build `target`; parse compiler, linker, and CMake output.

    Objects of the project's own sources are removed first so their warnings are
    reported on every build, not only when they change; SDK objects stay cached.
    """
    sdk = sdk_path()
    cmake = _tool("cmake")
    _tool("arm-none-eabi-gcc")
    env = {**os.environ, "PICO_SDK_PATH": str(sdk)}
    build_dir.mkdir(parents=True, exist_ok=True)
    configure = [cmake, "-S", str(source), "-B", str(build_dir), f"-DPICO_BOARD={board}"]
    log = _run(configure, env)
    messages = parse(log.stdout + log.stderr)
    if log.returncode != 0:
        return _failed(log, messages, "cmake configure failed")
    _forget_objects(source, build_dir)
    jobs = str(os.cpu_count() or 2)
    made = _run([cmake, "--build", str(build_dir), "--target", target, "-j", jobs], env)
    text = log.stdout + log.stderr + made.stdout + made.stderr
    messages = parse(text)
    elf = build_dir / f"{target}.elf"
    if made.returncode != 0 or not elf.is_file():
        return _failed(made, messages, "build failed", text)
    return BuildOutput(ok=True, log=text, messages=messages, elf=elf)


def size(elf: Path) -> dict[str, int]:
    """Section totals from `arm-none-eabi-size` (Berkeley format)."""
    proc = subprocess.run(
        [_tool("arm-none-eabi-size"), str(elf)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = proc.stdout.strip().splitlines()
    if proc.returncode != 0 or len(lines) < 2:
        raise PicoSdkError(f"arm-none-eabi-size failed: {proc.stderr.strip()}")
    text, data, bss = (int(v) for v in lines[1].split()[:3])
    return {"text": text, "data": data, "bss": bss}


def parse(log: str) -> list[Message]:
    """Errors and warnings from a build log, in order, without duplicates."""
    found: list[Message] = []
    seen: set[Message] = set()
    lines = log.splitlines()
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        message: Message | None = None
        if m := _COMPILER.match(line):
            kind = "warning" if m["kind"] == "warning" else "error"
            message = Message(
                kind, "compile", m["msg"], Path(m["file"]), int(m["line"]), int(m["col"])
            )
        elif m := _LINKER.match(line):
            message = Message("error", "link", m["msg"], Path(m["file"]), int(m["line"]))
        elif m := _LINKER_NOLINE.match(line):
            message = Message("error", "link", m["msg"], Path(m["file"]))
        elif m := _CMAKE.match(line):
            detail = _cmake_detail(lines, i + 1)
            kind = "error" if m["kind"] == "Error" else "warning"
            message = Message(kind, "cmake", detail, Path(m["file"]), int(m["line"]))
        if message is not None and message not in seen:
            seen.add(message)
            found.append(message)
    return found


def _cmake_detail(lines: list[str], start: int) -> str:
    """The indented paragraph that follows a `CMake Error at ...` line."""
    detail: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if detail:
                break
            continue
        if not line.startswith(" "):
            break
        detail.append(line.strip())
    return " ".join(detail) or "CMake error"


def _forget_objects(source: Path, build_dir: Path) -> None:
    # CMake mirrors sources under the source dir as CMakeFiles/<target>.dir/<rel>.o.
    for file in source.rglob("*"):
        if file.suffix not in _SOURCE_SUFFIXES or build_dir in file.parents:
            continue
        rel = file.relative_to(source).as_posix()
        for obj in build_dir.glob(f"CMakeFiles/*.dir/{rel}.o"):
            obj.unlink()


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise PicoSdkError(f"{' '.join(command[:3])} timed out after {TIMEOUT_S} s") from exc


def _failed(
    proc: subprocess.CompletedProcess[str],
    messages: list[Message],
    what: str,
    text: str | None = None,
) -> BuildOutput:
    log = text if text is not None else proc.stdout + proc.stderr
    if not any(m.kind == "error" for m in messages):
        tail = [line for line in log.strip().splitlines() if line.strip()][-5:]
        messages = [*messages, Message("error", "build", f"{what}: " + " | ".join(tail))]
    return BuildOutput(ok=False, log=log, messages=messages)
