from pathlib import Path

from eda.loader import load_yaml, validate
from eda.result import Diagnostics
from eda.schema.project import Project


def _load(tmp_path: Path, text: str) -> tuple[Diagnostics, object]:
    file = tmp_path / "f.yaml"
    file.write_text(text, encoding="utf-8")
    diag = Diagnostics()
    doc = load_yaml(file, "f.yaml", diag)
    return diag, doc


def test_positions_are_one_based_lines(tmp_path: Path) -> None:
    file = tmp_path / "f.yaml"
    file.write_text("a: 1\nb:\n  - x\n  - y\n", encoding="utf-8")
    doc = load_yaml(file, "f.yaml", Diagnostics())
    assert doc is not None
    assert doc.data == {"a": 1, "b": ["x", "y"]}
    loc = doc.source.at("b", 1)
    assert (loc.line, loc.column, loc.path) == (4, 5, "b[1]")
    assert doc.source.at("b", 1, "missing").line == 4  # falls back to nearest ancestor


def test_yaml_12_keeps_no_as_string(tmp_path: Path) -> None:
    diag, doc = _load(tmp_path, "answer: no\n")
    assert diag.ok
    assert doc is not None


def test_duplicate_key_is_error(tmp_path: Path) -> None:
    diag, doc = _load(tmp_path, "a: 1\na: 2\n")
    assert doc is None
    assert diag.errors[0].rule == "yaml_syntax"
    assert diag.errors[0].line == 2


def test_syntax_error_has_line(tmp_path: Path) -> None:
    diag, _ = _load(tmp_path, "a: [1, 2\nb: 3\n")
    assert diag.errors[0].rule == "yaml_syntax"
    assert diag.errors[0].line is not None


def test_missing_file(tmp_path: Path) -> None:
    diag = Diagnostics()
    assert load_yaml(tmp_path / "nope.yaml", "nope.yaml", diag) is None
    assert diag.errors[0].rule == "file_missing"


def test_schema_errors_point_at_field(tmp_path: Path) -> None:
    file = tmp_path / "project.yaml"
    file.write_text("name: ok\nextra: 1\n", encoding="utf-8")
    diag = Diagnostics()
    doc = load_yaml(file, "project.yaml", diag)
    assert doc is not None
    assert validate(Project, doc, diag) is None
    error = diag.errors[0]
    assert (error.rule, error.line, error.path) == ("schema", 2, "extra")
    assert "unknown field" in error.message
