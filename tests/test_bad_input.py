"""Regression tests for bad and extreme input: each case once crashed, hung, or was accepted."""

import json
from pathlib import Path

import pytest

import eda.project
from eda import vendor
from eda.cli import main
from tests.conftest import requires_ngspice
from tests.test_m1 import MODEL


@pytest.fixture
def vendor_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point parts/vendor/ at a temporary directory."""
    dest = tmp_path / "vendor"
    monkeypatch.setattr(vendor, "VENDOR_PARTS_DIR", dest)
    monkeypatch.setattr(eda.project, "VENDOR_PARTS_DIR", dest)
    return dest


def _cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def _rules(result: dict[str, object]) -> list[str]:
    errors = result["errors"]
    assert isinstance(errors, list)
    return [e["rule"] for e in errors]  # pyright: ignore[reportUnknownVariableType]


@pytest.mark.parametrize("part_id", ["bad id", "../escaped", "1abc", "abc\n", "resistor"])
def test_import_rejects_bad_ids_before_writing(
    part_id: str, tmp_path: Path, vendor_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "demoamp.lib"
    source.write_text(MODEL, encoding="utf-8")
    code, result = _cli(["model", "import", str(source), "--id", part_id], capsys)
    assert code == 1
    assert _rules(result) == ["import_failed"]
    assert not list(tmp_path.rglob("*.yaml"))  # nothing written, inside or outside vendor/


@requires_ngspice
def test_failed_import_leaves_parts_loadable(
    tmp_path: Path, vendor_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "demoamp.lib"
    source.write_text(MODEL, encoding="utf-8")
    argv = ["model", "import", str(source), "--id", "demoamp", "--opamp", "1,2,3,4,5"]
    code, result = _cli([*argv, "--optran", "abc"], capsys)
    assert code == 1
    assert _rules(result) == ["import_failed"]
    assert "optran" in str(result["errors"])
    assert not vendor_dir.exists() or not list(vendor_dir.glob("*.yaml"))
    code, result = _cli(["part", "list"], capsys)
    assert code == 0, result["errors"]
    code, result = _cli(argv, capsys)  # a good import still works
    assert code == 0, result["errors"]
    assert (vendor_dir / "demoamp.yaml").is_file()
