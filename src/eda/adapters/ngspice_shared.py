"""Thin wrapper around libngspice (the shared-library build of ngspice) for co-simulation.

The batch executable (`adapters/ngspice.py`) runs a whole analysis at once. Co-simulation
needs to stop a transient run at a time, read node voltages, change sources, and resume;
the shared library does that in-process with interactive commands
(`stop when`, `alter`, `resume`).

ngspice keeps global state, so there is one `Session` per process at a time.
"""

import ctypes
import ctypes.util
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from eda.adapters import ngspice

_SEARCH = (
    "/usr/local/lib/libngspice.so",
    "/usr/lib/libngspice.so",
    "/usr/lib/x86_64-linux-gnu/libngspice.so.0",
)

_SendChar = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_SendStat = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_Exit = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p
)


class _VectorInfo(ctypes.Structure):
    _fields_ = (
        ("v_name", ctypes.c_char_p),
        ("v_type", ctypes.c_int),
        ("v_flags", ctypes.c_short),
        ("v_realdata", ctypes.POINTER(ctypes.c_double)),
        ("v_compdata", ctypes.c_void_p),
        ("v_length", ctypes.c_int),
    )


class SharedSpiceError(RuntimeError):
    pass


class _Library:
    """The loaded library and its callbacks (kept alive for the life of the process)."""

    def __init__(self, path: str) -> None:
        self.lib = ctypes.CDLL(path)
        self.lib.ngGet_Vec_Info.restype = ctypes.POINTER(_VectorInfo)
        self.lib.ngGet_Vec_Info.argtypes = (ctypes.c_char_p,)
        self.lib.ngSpice_Command.argtypes = (ctypes.c_char_p,)
        self.lib.ngSpice_Circ.argtypes = (ctypes.POINTER(ctypes.c_char_p),)
        self.lib.ngSpice_SetBkpt.argtypes = (ctypes.c_double,)
        self.lib.ngSpice_SetBkpt.restype = ctypes.c_bool
        self.lib.ngSpice_CurPlot.restype = ctypes.c_char_p
        self.lib.ngSpice_AllVecs.argtypes = (ctypes.c_char_p,)
        self.lib.ngSpice_AllVecs.restype = ctypes.POINTER(ctypes.c_char_p)
        self.output: list[str] = []
        self.exited = False
        self._callbacks = (_SendChar(self._on_char), _SendStat(self._on_stat), _Exit(self._on_exit))
        # ngSpice_nospiceinit() (the `-n` of the batch runs) crashes ngspice 47 when called
        # before ngSpice_Init, as its documentation says it must be; it is not used.
        self.lib.ngSpice_Init(*self._callbacks, None, None, None, None)

    def _on_char(self, text: bytes | None, _id: int, _user: int | None) -> int:
        if text is not None:
            self.output.append(text.decode("utf-8", "replace"))
        return 0

    def _on_stat(self, _text: bytes | None, _id: int, _user: int | None) -> int:
        return 0

    def _on_exit(
        self, _status: int, _unload: bool, _quit: bool, _id: int, _user: int | None
    ) -> int:
        self.exited = True
        return 0


_library: _Library | None = None
_lock = threading.Lock()


def library_path() -> str | None:
    env = os.environ.get("EDA_LIBNGSPICE")
    if env:
        return env if Path(env).is_file() else None
    for candidate in _SEARCH:
        if Path(candidate).is_file():
            return candidate
    return ctypes.util.find_library("ngspice")


def _load() -> _Library:
    global _library
    if _library is None:
        path = library_path()
        if path is None:
            raise SharedSpiceError("libngspice not found (build ngspice with --with-ngshared)")
        try:
            _library = _Library(path)
        except OSError as exc:
            raise SharedSpiceError(f"cannot load {path}: {exc}") from exc
    return _library


class Session:
    """One loaded circuit. Use through `session()`."""

    def __init__(self, library: _Library) -> None:
        self._lib = library

    def command(self, text: str) -> None:
        start = len(self._lib.output)
        status = self._lib.lib.ngSpice_Command(text.encode())
        errors = self.errors(start)
        if status != 0 or errors or self._lib.exited:
            detail = "; ".join(errors[:3]) or f"status {status}"
            raise SharedSpiceError(f"ngspice `{text}` failed: {detail}")

    def errors(self, start: int = 0) -> list[str]:
        found: list[str] = []
        for line in self._lib.output[start:]:
            text = line.removeprefix("stderr ").strip()
            lowered = text.lower()
            if lowered.startswith("error") or " error:" in lowered or "fatal" in lowered:
                found.append(text)
        return found

    def warnings(self) -> list[str]:
        return ngspice.classify_log("\n".join(self._lib.output))[1]

    @property
    def op_via_transient(self) -> bool:
        """The operating point came from a transient run (ngspice optran)."""
        return ngspice.classify_log("\n".join(self._lib.output))[2]

    @property
    def log(self) -> str:
        return "\n".join(line.removeprefix("stdout ") for line in self._lib.output) + "\n"

    def last(self, vector: str) -> float:
        """The latest value of a real vector of the current plot (e.g. `time`, `v(out)`)."""
        info = self._lib.lib.ngGet_Vec_Info(vector.encode())
        if not info or not info.contents.v_realdata or info.contents.v_length < 1:
            raise SharedSpiceError(f"ngspice has no vector {vector!r}")
        data = info.contents
        return float(data.v_realdata[data.v_length - 1])

    def start(self, analysis: str, until: float) -> float:
        """Start `analysis` (a `tran` command) and halt at time `until`."""
        self._halt_at(until)
        start = len(self._lib.output)
        self.command(analysis)
        return self._halted(start, until)

    def run_until(self, until: float) -> float:
        """Resume the halted analysis up to time `until` (a time point lands exactly there)."""
        self.command("delete all")
        self._halt_at(until)
        start = len(self._lib.output)
        self.command("resume")
        return self._halted(start, until)

    def _halt_at(self, until: float) -> None:
        # The breakpoint makes the time-step control land on it; `stop` halts there.
        # ngspice lands a few ULPs short of a breakpoint, so the condition cannot simply
        # be `time >= until`: the landing would miss it and the run would go on for a
        # whole maximum step. Nor can it have a tolerance: a halt short of `until` leaves
        # a gap below ngspice's minimum step, and resuming fails ("timestep too small").
        # So the breakpoint sits a hair past `until` (1e-13 of it) and the condition is
        # exact: the landing is at or past `until`, and nothing is left before it.
        self._lib.lib.ngSpice_SetBkpt(until * (1 + 1e-13))
        self.command(f"stop when time >= {until:.17g}")

    def _halted(self, start: int, until: float) -> float:
        """The time the analysis stopped at; an error if ngspice gave up on the way."""
        for line in self._lib.output[start:]:
            text = line.removeprefix("stderr ").strip()
            if "timestep too small" in text.lower() or "simulation aborted" in text.lower():
                raise SharedSpiceError(f"ngspice stopped before {until:.6g} s: {text}")
        return self.last("time")

    def save(self, vectors: list[str]) -> None:
        """Keep only these vectors (and time) of the next analysis, e.g. `v(out)`,
        `i(v1)`. Sent in several commands: ngspice cuts long command lines."""
        line: list[str] = []
        for name in vectors:
            if line and len(" ".join(line)) + len(name) > 400:
                self.command("save " + " ".join(line))
                line = []
            line.append(name)
        if line:
            self.command("save " + " ".join(line))

    def write_raw(self, path: Path, *, grid: bool = False) -> None:
        """The saved vectors as an ASCII raw file (see ngspice.parse_raw). `grid`
        interpolates them onto the tran command's step (`linearize`) instead of every
        time point ngspice took."""
        if grid:
            self.command("linearize")
        self.command("set filetype=ascii")
        self.command(f"write {path.as_posix()} all")


@contextmanager
def session(netlist: str, pspice_mode: bool = False) -> Generator[Session]:
    """Load `netlist` (no .control block) into libngspice; removed again on exit."""
    with _lock:
        library = _load()
        library.output.clear()
        library.exited = False
        sim = Session(library)
        if pspice_mode:
            sim.command("set ngbehavior=ps")
        lines = [line.encode() for line in netlist.splitlines() if line.strip()]
        array = (ctypes.c_char_p * (len(lines) + 1))(*lines, None)
        library.lib.ngSpice_Circ(array)
        errors = sim.errors()
        if errors:
            raise SharedSpiceError("; ".join(errors[:5]))
        try:
            yield sim
        finally:
            library.lib.ngSpice_Command(b"destroy all")
            library.lib.ngSpice_Command(b"remcirc")
            library.lib.ngSpice_Command(b"unset ngbehavior")
