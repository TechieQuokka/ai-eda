"""Regression tests for bad and extreme input: each case once crashed, hung, or was accepted."""

import json
import shutil
from pathlib import Path
from textwrap import dedent

import pytest

import eda.project
from eda import pipeline, vendor
from eda.cli import main
from eda.design import load_design
from eda.loader import Document, load_yaml
from eda.result import Diagnostics
from tests.conftest import LED_CIRCUIT, MakeProject, requires_ngspice
from tests.test_m1 import MODEL

EXAMPLES = Path(__file__).resolve().parent.parent / "projects"


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


def _load(tmp_path: Path, text: str) -> tuple[object, list[str]]:
    path = tmp_path / "doc.yaml"
    path.write_text(text, encoding="utf-8")
    diag = Diagnostics()
    doc = load_yaml(path, "doc.yaml", diag)
    return doc, [e.rule for e in diag.errors]


def test_alias_bomb_is_refused(tmp_path: Path) -> None:
    lines = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    for prev, name in zip("abcdefgh", "bcdefghi", strict=True):
        lines.append(f"{name}: &{name} [" + ", ".join([f"*{prev}"] * 9) + "]")
    doc, rules = _load(tmp_path, "\n".join(lines) + "\n")  # 9**9 = 387 million values
    assert doc is None
    assert rules == ["yaml_too_large"]


@pytest.mark.parametrize("depth", [60, 5000])
def test_deep_nesting_is_refused(tmp_path: Path, depth: int) -> None:
    doc, rules = _load(tmp_path, "parts: " + "[" * depth + "]" * depth + "\n")
    assert doc is None
    assert rules == ["yaml_too_deep"]


def test_merge_key_gets_the_parent_position(tmp_path: Path) -> None:
    doc, rules = _load(tmp_path, "base: &b { type: capacitor }\nC1: { <<: *b, capacitance: 1nF }\n")
    assert rules == []
    assert isinstance(doc, Document)
    assert doc.data == {
        "base": {"type": "capacitor"},
        "C1": {"capacitance": "1nF", "type": "capacitor"},
    }
    assert doc.source.at("C1", "type").line == 2


@pytest.mark.parametrize(
    "analysis",
    [
        "{ type: tran, step: 1ns, stop: 10s }",
        "{ type: tran, step: 1us, stop: 2s }",
        "{ type: ac, start: 1e-90Hz, stop: 1THz, points_per_decade: 1000 }",
    ],
)
def test_runaway_analysis_is_refused(make_project: MakeProject, analysis: str) -> None:
    tests = f"""
    tests:
      big:
        analysis: {analysis}
        expect:
          - {{ measure: max(voltage(VCC)), max: 10V }}
    """
    measure = "max(voltage(VCC))" if "tran" in analysis else '"at(db(voltage(VCC)), 1kHz)"'
    unit = "V" if "tran" in analysis else "dB"
    tests = tests.replace("max(voltage(VCC)), max: 10V", f"{measure}, max: 10{unit}")
    circuit = LED_CIRCUIT.replace("voltage: 5V }", "voltage: 5V, ac: 1V }")
    diag = Diagnostics()
    assert load_design(make_project(circuit, tests), diag) is None
    assert [e.rule for e in diag.errors] == ["too_many_points"]


def test_firmware_cosim_length_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "pico_io"
    shutil.copytree(EXAMPLES / "pico_io", root, ignore=shutil.ignore_patterns("out", "*.jsonl"))
    (root / "design" / "test.yaml").write_text(
        dedent("""
        tests:
          long:
            analysis: { type: tran, step: 10us, stop: 2s }
            firmware: true
            expect:
              - { measure: max(current(D1)), min: 5mA }
        """),
        encoding="utf-8",
    )
    diag = Diagnostics()
    assert load_design(root, diag) is None
    assert [e.rule for e in diag.errors] == ["too_many_points"]
    assert "co-simulation" in diag.errors[0].message


def test_board_beyond_one_metre_is_refused(make_project: MakeProject) -> None:
    root = make_project()
    (root / "design" / "board.yaml").write_text(
        "board: { width: 5000mm, height: 20mm }\nplacement:\n  V1: { x: -2m, y: 1mm }\n",
        encoding="utf-8",
    )
    diag = Diagnostics()
    assert load_design(root, diag) is None
    assert [e.message for e in diag.errors] == [
        "board.width = 5000mm is beyond 1000 mm",
        "placement.V1.x = -2m is beyond 1000 mm",
    ]


@pytest.mark.parametrize(
    ("extra_part", "extra_pins"),
    [
        ("V2: { type: dc_voltage_source, voltage: 3V }", ("V2.+", "V2.-")),
        ("L1: { type: inductor, inductance: 1mH }", ("L1.1", "L1.2")),
    ],
)
def test_source_or_inductor_across_a_source_is_a_dc_loop(
    make_project: MakeProject, extra_part: str, extra_pins: tuple[str, str]
) -> None:
    circuit = (
        LED_CIRCUIT.replace("  D1:", f"  {extra_part}\n  D1:")
        .replace("[V1.+, R1.1]", f"[V1.+, R1.1, {extra_pins[0]}]")
        .replace("[D1.cathode, V1.-]", f"[D1.cathode, V1.-, {extra_pins[1]}]")
    )
    diag = Diagnostics()
    assert load_design(make_project(circuit), diag) is None
    assert [e.rule for e in diag.errors] == ["dc_loop"]
    assert diag.errors[0].details["parts"] == ["V1", extra_pins[0].split(".")[0]]


def test_series_sources_and_lc_filters_are_not_loops(make_project: MakeProject) -> None:
    circuit = """
    parts:
      V1: { type: dc_voltage_source, voltage: 5V }
      V2: { type: dc_voltage_source, voltage: 1V }
      L1: { type: inductor, inductance: 1mH }
      C1: { type: capacitor, capacitance: 1uF }
      R1: { type: resistor, resistance: 1kohm }
    nets:
      A: [V1.+, V2.-]
      B: [V2.+, L1.1]
      OUT: [L1.2, C1.1, R1.1]
      GND: [V1.-, C1.2, R1.2]
    """
    tests = """
    tests:
      dc:
        analysis: { type: op }
        expect:
          - { measure: voltage(OUT), min: 5.9V, max: 6.1V }
    """
    diag = Diagnostics()
    assert load_design(make_project(circuit, tests), diag) is not None, diag.errors


@requires_ngspice
def test_singular_matrix_results_are_not_evaluated(make_project: MakeProject) -> None:
    # A resistor template with a capacitor's element letter: ngspice reads "C... 1000", so
    # the node between two of them is held by nothing at DC.
    circuit = """
    parts:
      V1: { type: dc_voltage_source, voltage: 5V }
      R1: { type: bad_r, resistance: 1kohm }
      R2: { type: bad_r, resistance: 1kohm }
    nets:
      IN: [V1.+, R1.1]
      MID: [R1.2, R2.1]
      GND: [R2.2, V1.-]
    """
    tests = """
    tests:
      dc:
        analysis: { type: op }
        expect:
          - { measure: voltage(MID), min: 2.4V, max: 2.6V }
    """
    root = make_project(circuit, tests)
    (root / "parts").mkdir()
    spec = (EXAMPLES.parent / "parts" / "resistor.yaml").read_text(encoding="utf-8")
    (root / "parts" / "bad_r.yaml").write_text(
        spec.replace("id: resistor", "id: bad_r").replace("prefix: R", "prefix: C"),
        encoding="utf-8",
    )
    result = pipeline.sim(root)
    assert [e.rule for e in result.errors] == ["singular_matrix"]
    assert "near node mid" in result.errors[0].message
