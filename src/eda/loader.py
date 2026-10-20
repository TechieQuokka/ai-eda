"""Strict YAML loading with line-precise positions, and pydantic validation on top.

YAML is parsed by ruamel.yaml (YAML 1.2: `no` stays a string, duplicate keys are errors).
Every mapping key and sequence item gets a recorded position so later stages can point
errors at the exact line.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from eda.result import Diagnostics, Location

type PathKey = str | int
type DataPath = tuple[PathKey, ...]
type Plain = str | int | float | bool | list[Plain] | dict[str, Plain] | None


def format_path(path: Sequence[PathKey]) -> str:
    """Render a data path as `tests.led.expect[0].min`."""
    out = ""
    for key in path:
        if isinstance(key, int):
            out += f"[{key}]"
        else:
            out += f".{key}" if out else key
    return out


@dataclass(frozen=True)
class SourceMap:
    """Positions (1-based line, column) of every node in one YAML file."""

    file: str
    positions: dict[DataPath, tuple[int, int]]

    def at(self, *path: PathKey) -> Location:
        """Location of `path`, falling back to the nearest ancestor with a known position."""
        probe: DataPath = tuple(path)
        while probe not in self.positions and probe:
            probe = probe[:-1]
        line, column = self.positions.get(probe, (1, 1))
        return Location(file=self.file, line=line, column=column, path=format_path(path) or None)


@dataclass(frozen=True)
class Document:
    data: Plain
    source: SourceMap


class _ConversionError(Exception):
    def __init__(self, message: str, path: DataPath) -> None:
        super().__init__(message)
        self.path = path


def load_yaml(path: Path, display_name: str, diag: Diagnostics) -> Document | None:
    """Parse one YAML file. On failure, record errors and return None."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        diag.error("file_missing", f"{display_name} not found", Location(file=display_name))
        return None
    except (OSError, UnicodeDecodeError) as exc:
        diag.error("file_unreadable", f"cannot read {display_name}: {exc}", Location(display_name))
        return None

    yaml: Any = YAML(typ="rt", pure=True)
    yaml.allow_duplicate_keys = False
    try:
        raw: object = yaml.load(text)
    except MarkedYAMLError as marked:
        exc: Any = marked  # ruamel's error attributes are untyped
        mark: Any = exc.problem_mark or exc.context_mark
        line = int(mark.line) + 1 if mark is not None else None
        column = int(mark.column) + 1 if mark is not None else None
        texts: list[object] = [exc.context, exc.problem]
        problem = " ".join(str(p) for p in texts if p)
        diag.error(
            "yaml_syntax",
            f"YAML syntax error: {problem or exc}",
            Location(file=display_name, line=line, column=column),
        )
        return None
    except YAMLError as exc:
        diag.error("yaml_syntax", f"YAML error: {exc}", Location(file=display_name))
        return None

    positions: dict[DataPath, tuple[int, int]] = {(): (1, 1)}
    try:
        data = _convert(raw, (), positions)
    except _ConversionError as exc:
        source = SourceMap(display_name, positions)
        diag.error("yaml_type", str(exc), source.at(*exc.path))
        return None
    return Document(data=data, source=SourceMap(display_name, positions))


def _convert(node: object, path: DataPath, positions: dict[DataPath, tuple[int, int]]) -> Plain:
    # ruamel's node classes are untyped; treat them as Any inside the isinstance branches.
    if isinstance(node, CommentedMap):
        mapping: Any = node
        result: dict[str, Plain] = {}
        for key, value in mapping.items():
            if not isinstance(key, str):
                raise _ConversionError(f"mapping keys must be strings, got {key!r}", path)
            child = (*path, key)
            line, column = mapping.lc.key(key)
            positions[child] = (int(line) + 1, int(column) + 1)
            result[key] = _convert(value, child, positions)
        return result
    if isinstance(node, CommentedSeq):
        sequence: Any = node
        items: list[Plain] = []
        for index, value in enumerate(sequence):
            child = (*path, index)
            line, column = sequence.lc.item(index)
            positions[child] = (int(line) + 1, int(column) + 1)
            items.append(_convert(value, child, positions))
        return items
    if node is None or isinstance(node, bool):
        return node
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    raise _ConversionError(
        f"unsupported YAML value {node!r} ({type(node).__name__}); quote it as a string", path
    )


def validate[M: BaseModel](model: type[M], document: Document, diag: Diagnostics) -> M | None:
    """Validate a whole document against a pydantic model; errors point at YAML lines."""
    try:
        return model.model_validate(document.data, strict=True)
    except ValidationError as exc:
        for err in exc.errors(include_url=False):
            loc: DataPath = tuple(err["loc"])
            diag.error(
                "schema",
                _schema_message(err["type"], err["msg"], loc),
                document.source.at(*loc),
                type=err["type"],
            )
        return None


def _schema_message(kind: str, message: str, loc: DataPath) -> str:
    where = format_path(loc) or "document"
    if kind == "extra_forbidden":
        return f"unknown field {where!r}"
    if kind == "missing":
        return f"missing required field {where!r}"
    return f"{where}: {message}"
