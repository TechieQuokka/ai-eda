"""Thin wrapper around the Freerouting autorouter (Specctra .dsn in, .ses out)."""

import os
import shutil
import subprocess
from pathlib import Path

from eda.project import REPO_ROOT

TOOL_DIR = REPO_ROOT / "tools" / "freerouting"
JAR = TOOL_DIR / "dl" / "freerouting-2.1.0.jar"
DATA_DIR = TOOL_DIR / "data"  # keeps Freerouting's settings and logs inside the repo
TIMEOUT_S = 180  # small boards route in seconds; fail clearly instead of hanging


class FreeroutingUnavailable(RuntimeError):
    pass


def route(dsn: Path, ses: Path, passes: int, log: Path) -> None:
    java = os.environ.get("EDA_JAVA") or shutil.which("java")
    if java is None:
        raise FreeroutingUnavailable("java not found on PATH (Freerouting needs Java 21)")
    if not JAR.is_file():
        raise FreeroutingUnavailable(f"Freerouting is not installed; run {TOOL_DIR}/fetch.sh")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ses.unlink(missing_ok=True)
    env = {**os.environ, "FREEROUTING__USAGE_AND_DIAGNOSTIC_DATA__DISABLE_ANALYTICS": "true"}
    command = [
        java, "-jar", str(JAR),
        "-de", str(dsn), "-do", str(ses), "-mp", str(passes), "-mt", "1",
        "-da", "--gui.enabled=false", f"--user_data_path={DATA_DIR}",
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Freerouting did not finish within {TIMEOUT_S} s; try again, reduce "
            "routing.passes, or give the parts more room"
        ) from exc
    log.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if not ses.is_file():
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise RuntimeError("Freerouting produced no session: " + " | ".join(tail))
