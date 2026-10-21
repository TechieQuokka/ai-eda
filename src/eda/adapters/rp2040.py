"""Thin wrapper around rp2040js (Wokwi's RP2040 emulator, Node), pinned in `tools/rp2040/`.

`rp2040_driver.mjs` runs the firmware and answers line-delimited JSON requests; this
module starts it and exchanges pin states, ADC codes, and UART output per time window.
The RP2040 boot ROM is fetched separately (`tools/rp2040/fetch-bootrom.sh`).
"""

import json
import os
import select
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from eda.project import REPO_ROOT

TOOL_DIR = REPO_ROOT / "tools" / "rp2040"
PACKAGE_DIR = TOOL_DIR / "node_modules" / "rp2040js"
BOOTROM = TOOL_DIR / "dl" / "bootrom-b2.bin"
DRIVER = Path(__file__).with_name("rp2040_driver.mjs")
TIMEOUT_S = 60  # longest one request may take

type Segments = list[tuple[float, str]]  # (start time in s, pin state), first at window start


class EmulatorError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    end: float  # s
    pins: dict[int, Segments]  # GPIO number → states over the window
    uart: dict[int, str]  # UART index → text sent during the window
    halted: int | None  # BKPT code once the firmware stopped (e.g. returned from main)
    halted_at: float | None  # s, when it stopped
    faults: list[tuple[float, str]]  # (s, message): accesses to unmapped memory
    early: bool  # stopped before `until` because a pin changed mode (see run)


def bootrom() -> Path:
    found = Path(os.environ.get("EDA_RP2040_BOOTROM") or BOOTROM)
    if not found.is_file():
        raise EmulatorError(
            f"RP2040 boot ROM not found at {found}; run {TOOL_DIR}/fetch-bootrom.sh"
        )
    return found


class Emulator:
    """One running firmware image. Use as a context manager."""

    def __init__(self, image: Path, pins: list[int], uarts: list[int], log: Path) -> None:
        node = shutil.which("node")
        if node is None:
            raise EmulatorError("node not found on PATH (needed for the RP2040 emulator)")
        if not (PACKAGE_DIR / "package.json").is_file():
            raise EmulatorError(f"rp2040js is not installed; run: npm --prefix {TOOL_DIR} ci")
        rom = bootrom()
        self._log = log
        self._log_file = log.open("w", encoding="utf-8")  # emulator warnings
        self._proc = subprocess.Popen(
            [node, str(DRIVER), str(PACKAGE_DIR)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log_file,
            text=True,
        )
        self._request(
            {"cmd": "init", "bootrom": str(rom), "image": str(image), "pins": pins, "uarts": uarts}
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._proc.poll() is None:
            assert self._proc.stdin is not None
            self._proc.stdin.close()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._log_file.close()

    def run(
        self,
        until: float,
        inputs: dict[int, bool],
        adc: dict[int, int],
        break_on_mode: bool,
        send: dict[int, str] | None = None,
    ) -> Window:
        """Run to `until` seconds with these GPIO input levels and ADC channel codes.

        With `break_on_mode`, return early right after a pin changes mode (pull-up enabled,
        output enabled, ...): such a change can move the pin's own input level. `send`
        queues text for UART receivers (paced at the firmware's baud rate)."""
        reply = self._request(
            {
                "cmd": "run",
                "until": round(until * 1e9),
                "inputs": {str(k): v for k, v in inputs.items()},
                "adc": {str(k): v for k, v in adc.items()},
                "break_on_mode": break_on_mode,
                "send": {str(k): v for k, v in (send or {}).items()},
            }
        )
        pins: dict[int, Segments] = {
            int(gpio): [(float(t) * 1e-9, str(state)) for t, state in segments]
            for gpio, segments in reply["pins"].items()
        }
        uart = {int(k): str(v) for k, v in reply["uart"].items()}
        halted = reply.get("halted")
        halted_at = reply.get("halted_at")
        return Window(
            end=float(reply["t"]) * 1e-9,
            pins=pins,
            uart=uart,
            halted=int(halted) if halted is not None else None,
            halted_at=float(halted_at) * 1e-9 if halted_at is not None else None,
            faults=[(float(t) * 1e-9, str(m)) for t, m in reply.get("faults", [])],
            early=bool(reply.get("early")),
        )

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        stdin, stdout = self._proc.stdin, self._proc.stdout
        assert stdin is not None and stdout is not None
        try:
            stdin.write(json.dumps(request) + "\n")
            stdin.flush()
            # One reply line per request, so nothing is left in stdout's buffer meanwhile.
            ready, _, _ = select.select([stdout], [], [], TIMEOUT_S)
            if not ready:
                self._proc.kill()
                raise EmulatorError(f"emulator did not answer within {TIMEOUT_S} s")
            line = stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            raise EmulatorError(f"emulator stopped: {self._stderr()}") from exc
        if not line:
            raise EmulatorError(f"emulator stopped: {self._stderr()}")
        reply: dict[str, Any] = json.loads(line)
        if not reply.get("ok"):
            raise EmulatorError(str(reply.get("error")))
        return reply

    def _stderr(self) -> str:
        self.close()
        tail = self._log.read_text(encoding="utf-8").strip()[-500:]
        return tail or f"exit code {self._proc.returncode}"
