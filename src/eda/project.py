"""Project layout and the part database."""

import fcntl
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from eda.loader import load_yaml, validate
from eda.result import Diagnostics, Location
from eda.schema.part import PartSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_PARTS_DIR = REPO_ROOT / "parts"
# Imported vendor models (`eda model import`); git-ignored because vendor licenses
# usually forbid redistribution.
VENDOR_PARTS_DIR = BUILTIN_PARTS_DIR / "vendor"


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def project_file(self) -> Path:
        return self.root / "project.yaml"

    @property
    def circuit_file(self) -> Path:
        return self.root / "design" / "circuit.yaml"

    @property
    def test_file(self) -> Path:
        return self.root / "design" / "test.yaml"

    @property
    def board_file(self) -> Path:
        return self.root / "design" / "board.yaml"

    @property
    def firmware_file(self) -> Path:
        return self.root / "design" / "firmware.yaml"

    @property
    def parts_dir(self) -> Path:
        return self.root / "parts"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def log_file(self) -> Path:
        return self.root / "iterations.jsonl"

    def rel(self, path: Path) -> str:
        """Path relative to the project root, for messages."""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def design_files(self) -> list[Path]:
        """Files whose changes define a design iteration."""
        files = [
            self.project_file,
            self.circuit_file,
            self.test_file,
            self.board_file,
            self.firmware_file,
        ]
        if self.parts_dir.is_dir():
            files.extend(sorted(self.parts_dir.glob("*.yaml")))
        return [f for f in files if f.is_file()]


@contextmanager
def project_lock(paths: ProjectPaths) -> Generator[None]:
    """Hold the project's lock: commands on one project (a manual `eda run` while
    `eda watch` re-runs) take turns instead of overwriting each other's files in out/."""
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    with (paths.out_dir / ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # released when the file closes
        yield


_CONTROL = re.compile(r"^\s*\.(control|exec)\b", re.IGNORECASE | re.MULTILINE)
_NESTED = re.compile(r"^\s*\.(?:include|inc|lib)\s+[\"']?([^\s\"']+)", re.IGNORECASE | re.MULTILINE)


def model_hazard(path: Path, depth: int = 0) -> str | None:
    """Why a SPICE model file must not be simulated, or None. A .control or .exec block in
    a model runs ngspice commands, `shell` among them, so a downloaded model could run
    anything; files it includes are checked too."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"cannot read model file {path.name}: {exc}"
    found = _CONTROL.search(text)
    if found:
        line = text.count("\n", 0, found.start()) + 1
        return (
            f"{path.name} line {line} has a .{found[1].lower()} block, which would run "
            "simulator commands; remove it from the model file"
        )
    if depth < 5:
        for name in _NESTED.findall(text):
            nested = path.parent / name
            if nested.is_file() and (problem := model_hazard(nested, depth + 1)):
                return problem
    return None


def find_project(start: Path) -> Path | None:
    """The nearest directory at or above `start` containing `project.yaml`."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "project.yaml").is_file():
            return candidate
    return None


def load_parts(
    diag: Diagnostics, project_parts: Path | None = None, project_root: Path | None = None
) -> dict[str, PartSpec]:
    """Load built-in parts, then project-local parts. Duplicate ids are errors."""
    specs: dict[str, PartSpec] = {}
    origins: dict[str, str] = {}
    sources: list[tuple[Path, str]] = [
        (f, f"<builtin>/parts/{f.name}") for f in sorted(BUILTIN_PARTS_DIR.glob("*.yaml"))
    ]
    sources += [
        (f, f"<builtin>/parts/vendor/{f.name}") for f in sorted(VENDOR_PARTS_DIR.glob("*.yaml"))
    ]
    if project_parts is not None and project_parts.is_dir():
        for f in sorted(project_parts.glob("*.yaml")):
            shown = f.relative_to(project_root).as_posix() if project_root else f.as_posix()
            sources.append((f, shown))

    for file, shown in sources:
        doc = load_yaml(file, shown, diag)
        if doc is None:
            continue
        spec = validate(PartSpec, doc, diag)
        if spec is None:
            continue
        if f"{spec.id}.yaml" != file.name:
            diag.error(
                "part_id",
                f"part id {spec.id!r} must match its file name {file.name!r}",
                doc.source.at("id"),
            )
            continue
        spec = spec.model_copy(update={"source_dir": str(file.parent.resolve())})
        if spec.include_path() is not None and not Path(str(spec.include_path())).is_file():
            diag.error(
                "model_missing",
                f"part {spec.id!r}: model file {spec.spice.include!r} not found",
                doc.source.at("spice", "include"),
            )
            continue
        hazard = model_hazard(Path(str(spec.include_path()))) if spec.spice.include else None
        if hazard is not None:
            diag.error(
                "unsafe_model",
                f"part {spec.id!r}: {hazard}",
                doc.source.at("spice", "include"),
            )
            continue
        if spec.id in specs:
            diag.error(
                "part_duplicate",
                f"part {spec.id!r} is already defined in {origins[spec.id]}",
                Location(file=shown, line=1, column=1),
            )
            continue
        specs[spec.id] = spec
        origins[spec.id] = shown
    return specs
