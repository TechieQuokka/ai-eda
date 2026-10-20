"""Iteration log (`iterations.jsonl`): what changed, what failed, how it was fixed."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from eda.project import ProjectPaths
from eda.result import Result

_STATE_FILE = ".design-hashes.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _append(paths: ProjectPaths, entry: dict[str, JsonValue]) -> None:
    with paths.log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def changed_files(paths: ProjectPaths) -> list[str]:
    """Design files changed since the previous logged run (hashes kept in `out/`)."""
    state_path = paths.out_dir / _STATE_FILE
    try:
        previous: dict[str, str] = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    current = {
        paths.rel(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in paths.design_files()
    }
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(current, indent=1), encoding="utf-8")
    names = sorted(set(previous) | set(current))
    return [n for n in names if previous.get(n) != current.get(n)]


def record_run(paths: ProjectPaths, result: Result, only_if_changed: bool = False) -> None:
    """Append a run entry. `only_if_changed` skips runs that saw no design change
    (used by `eda watch`, which often re-runs what an explicit command already logged)."""
    changed = changed_files(paths)
    if only_if_changed and not changed:
        return
    entry: dict[str, JsonValue] = {
        "time": _now(),
        "kind": "run",
        "command": result.command,
        "ok": result.ok,
        "changed": list(changed),
        "errors": [
            {"rule": e.rule, "message": e.message, "file": e.file, "line": e.line}
            for e in result.errors[:10]
        ],
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
    }
    _append(paths, entry)


def record_note(paths: ProjectPaths, message: str) -> dict[str, JsonValue]:
    entry: dict[str, JsonValue] = {"time": _now(), "kind": "note", "message": message}
    _append(paths, entry)
    return entry


def read_log(paths: ProjectPaths, limit: int = 200) -> list[JsonValue]:
    path: Path = paths.log_file
    if not path.is_file():
        return []
    entries: list[JsonValue] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries
