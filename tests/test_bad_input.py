"""Regression tests for bad and extreme input: each case once crashed, hung, or was accepted."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

import eda.project
from eda import eseries, pipeline, vendor
from eda.cli import main
from eda.design import load_design
from eda.loader import Document, load_yaml
from eda.project import ProjectPaths, project_lock
from eda.result import Diagnostics
from eda.schema.part import ParamSpec
from eda.units import format_quantity
from tests.conftest import LED_CIRCUIT, MakeProject, requires_ngspice
from tests.test_m1 import MODEL
from tests.test_m4 import requires_cosim

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


@requires_cosim
def test_firmware_faults_halts_and_uart_pacing_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "pico_io"
    shutil.copytree(EXAMPLES / "pico_io", root, ignore=shutil.ignore_patterns("out", "*.jsonl"))
    (root / "design" / "board.yaml").unlink()
    (root / "firmware" / "main.c").write_text(
        dedent("""
        #include <stdio.h>
        #include "pico/stdlib.h"
        #include "pins.h"
        #include "pins_init.h"

        int main(void) {
            pins_init();
            stdio_init_all();
            for (int i = 0; i < 100; i++) printf("0123456789\\n");  // 1200 bytes: ~104 ms
            *(volatile int *)0xFFFFFFF0 = 1;
            return 0;
        }
        """),
        encoding="utf-8",
    )
    (root / "design" / "test.yaml").write_text(
        dedent("""
        tests:
          crash:
            analysis: { type: tran, step: 1ms, stop: 200ms }
            firmware: true
            expect:
              - { measure: max(voltage(V3V3)), min: 3V }
        """),
        encoding="utf-8",
    )
    assert pipeline.fw(root, build=True).ok
    result = pipeline.sim(root)
    assert [e.rule for e in result.errors] == ["firmware_fault"]
    assert [w.rule for w in result.warnings] == ["firmware_halted"]
    tests = result.data["tests"]
    assert isinstance(tests, dict)
    crash = tests["crash"]
    assert isinstance(crash, dict) and crash["passed"] is False
    firmware = crash["firmware"]
    assert isinstance(firmware, dict)
    faults = firmware["faults"]
    assert isinstance(faults, list) and len(faults) == 1
    fault = faults[0]
    assert isinstance(fault, list)
    fault_at, message = fault
    assert isinstance(fault_at, float) and "fffffff0" in str(message)
    # Printing waits for the UART like on the chip: 1200 characters at 115200 baud.
    assert fault_at == pytest.approx(1200 * 10 / 115200, rel=0.1)
    halted_at = firmware["halted_at"]
    assert isinstance(halted_at, float) and fault_at <= halted_at < 0.2


def test_unexpected_exceptions_still_answer_in_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], make_project: MakeProject
) -> None:
    def boom(*_args: object) -> None:
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(pipeline, "check", boom)
    code, result = _cli(["check", str(make_project())], capsys)
    assert code == 2
    assert _rules(result) == ["internal_error"]
    assert "RuntimeError: simulated bug" in str(result["errors"])


def test_out_that_is_a_file(make_project: MakeProject, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_project()
    (root / "out").write_text("not a directory", encoding="utf-8")
    code, result = _cli(["run", str(root)], capsys)
    assert code == 2
    assert _rules(result) == ["out_not_a_directory"]


def test_unreadable_design_file_is_reported(
    make_project: MakeProject, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project()
    circuit = root / "design" / "circuit.yaml"
    circuit.chmod(0)
    try:
        if os.access(circuit, os.R_OK):
            pytest.skip("running as a user who can read anything")
        code, result = _cli(["check", str(root)], capsys)
    finally:
        circuit.chmod(0o644)
    assert code == 1
    assert _rules(result) == ["file_unreadable"]


def test_deeply_nested_measure(make_project: MakeProject) -> None:
    measure = "max(" * 10_000 + "voltage(VCC)" + ")" * 10_000
    tests = f"""
    tests:
      t:
        analysis: {{ type: tran, step: 1us, stop: 1ms }}
        expect:
          - {{ measure: "{measure}", max: 10V }}
    """
    diag = Diagnostics()
    assert load_design(make_project(tests=tests), diag) is None
    assert [e.rule for e in diag.errors] == ["invalid_measure"]
    assert "nested more than 20" in diag.errors[0].message


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("<name> <pin:1> <pin:2> <param:nope>", "no parameter 'nope'"),
        ("<name> <pin:1> <pin:9> <param:resistance>", "no pin '9'"),
        ("<name> <pin:1> <pin:2> <param:resistance> <foo>", "unknown placeholder <foo>"),
        ("<name> <pin:1> <pin:2> <choice:resistance>", "needs a choices parameter"),
        ("<name> <pin:1> <pin:2> <model>", "no model_param"),
    ],
)
def test_part_templates_are_checked_when_loaded(
    make_project: MakeProject, template: str, message: str
) -> None:
    root = make_project()
    (root / "parts").mkdir()
    spec = (EXAMPLES.parent / "parts" / "resistor.yaml").read_text(encoding="utf-8")
    spec = spec.replace("id: resistor", "id: odd_r").replace(
        '"<name> <pin:1> <pin:2> <param:resistance>"', json.dumps(template)
    )
    (root / "parts" / "odd_r.yaml").write_text(spec, encoding="utf-8")
    diag = Diagnostics()
    load_design(root, diag)
    assert [e.rule for e in diag.errors] == ["schema"]
    assert message in diag.errors[0].message


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("resistance: 330ohm", "resistance: 0ohm", "R1.resistance = 0Ω must be greater than zero"),
        (
            "resistance: 330ohm",
            "resistance: -1kohm",
            "R1.resistance = -1kΩ must be greater than zero",
        ),
        (
            "R1: { type: resistor, resistance: 330ohm }",
            "R1: { type: potentiometer, resistance: 1kohm, position: 120% }",
            "R1.position = 120% must be at most 100%",
        ),
    ],
)
def test_parameter_ranges(make_project: MakeProject, old: str, new: str, message: str) -> None:
    circuit = LED_CIRCUIT.replace(old, new)
    if "potentiometer" in new:
        circuit = circuit.replace("N1: [R1.2, D1.anode]", "N1: [R1.2, R1.3, D1.anode]")
    diag = Diagnostics()
    assert load_design(make_project(circuit), diag) is None
    assert [e.message for e in diag.errors if e.rule == "invalid_value"] == [message]


def test_parameter_ranges_apply_to_test_overrides(make_project: MakeProject) -> None:
    tests = """
    tests:
      t:
        analysis: { type: op }
        parts: { R1: { resistance: -5ohm } }
        expect:
          - { measure: current(D1), min: 9mA }
    """
    diag = Diagnostics()
    assert load_design(make_project(tests=tests), diag) is None
    assert [e.rule for e in diag.errors] == ["invalid_value"]


@pytest.mark.parametrize(
    ("param", "message"),
    [
        ({"unit": "ohm", "default": "banana"}, "invalid quantity 'banana'"),
        (
            {"unit": "ohm", "positive": True, "default": "0ohm"},
            "default 0ohm: must be greater than zero",
        ),
        ({"unit": "ohm", "min": "2ohm", "max": "1ohm"}, "min is greater than max"),
        ({"choices": ["a", "b"], "positive": True}, "apply to quantities"),
    ],
)
def test_parameter_specs_are_checked(param: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        ParamSpec.model_validate(param)


def _led_board(make_project: MakeProject, board: str, circuit: str = LED_CIRCUIT) -> list[str]:
    root = make_project(circuit)
    (root / "design" / "board.yaml").write_text(board, encoding="utf-8")
    diag = Diagnostics()
    load_design(root, diag)
    return [e.message for e in diag.errors]


def test_via_drill_must_leave_a_ring(make_project: MakeProject) -> None:
    messages = _led_board(make_project, "rules: { via_diameter: 0.4mm, via_drill: 0.8mm }\n")
    assert messages == [
        "rules.via_drill (0.8mm) must be smaller than rules.via_diameter (0.4mm), "
        "or the via has no copper ring"
    ]


def test_two_nets_on_one_pad(make_project: MakeProject) -> None:
    circuit = LED_CIRCUIT.replace("330ohm }", "330ohm, pads: { '1': '1', '2': '1' } }")
    assert _led_board(make_project, "board: {}\n", circuit) == [
        "R1: pad 1 is given to pins 1, 2 on different nets"
    ]


@requires_ngspice
def test_reversed_opamp_supply(make_project: MakeProject) -> None:
    circuit = """
    parts:
      VCC: { type: dc_voltage_source, voltage: 12V }
      VEE: { type: dc_voltage_source, voltage: 12V }
      U1: { type: opamp }
      R1: { type: resistor, resistance: 10kohm }
    nets:
      VP: [VCC.+, U1.v-]
      VN: [VEE.-, U1.v+]
      OUT: [U1.out, U1.in-, R1.1]
      GND: [VCC.-, VEE.+, U1.in+, R1.2]
    """
    tests = """
    tests:
      dc:
        analysis: { type: op }
        expect:
          - { measure: voltage(OUT), max: 1V }
    """
    result = pipeline.sim(make_project(circuit, tests))
    assert "supply_reversed" in [e.rule for e in result.errors]


def test_commands_on_one_project_take_turns(make_project: MakeProject) -> None:
    root = make_project()
    command = [sys.executable, "-m", "eda.cli", "check", str(root)]
    with project_lock(ProjectPaths(root)):
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=2)  # blocked on the lock we hold
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0
    assert json.loads(stdout)["ok"] is True


def test_template_cannot_smuggle_control_cards(make_project: MakeProject) -> None:
    root = make_project()
    (root / "parts").mkdir()
    spec = (EXAMPLES.parent / "parts" / "resistor.yaml").read_text(encoding="utf-8")
    spec = spec.replace("id: resistor", "id: odd_r").replace(
        '"<name> <pin:1> <pin:2> <param:resistance>"',
        json.dumps("<name> <pin:1> <pin:2> <param:resistance>\n.control\nshell touch x\n.endc"),
    )
    (root / "parts" / "odd_r.yaml").write_text(spec, encoding="utf-8")
    diag = Diagnostics()
    load_design(root, diag)
    assert [e.rule for e in diag.errors] == ["schema"]
    assert "line 2 is a control card (.control)" in diag.errors[0].message


@pytest.mark.parametrize("nested", [False, True])
def test_models_with_control_blocks_are_refused(
    make_project: MakeProject, tmp_path: Path, nested: bool
) -> None:
    root = make_project()
    models = root / "parts" / "models"
    models.mkdir(parents=True)
    evil = ".control\nshell touch pwned\n.endc\n"
    if nested:
        (models / "inner.lib").write_text(evil, encoding="utf-8")
        evil = '.include "inner.lib"\n'
    (models / "amp.lib").write_text(MODEL + evil, encoding="utf-8")
    spec = (EXAMPLES.parent / "parts" / "opamp.yaml").read_text(encoding="utf-8")
    (root / "parts" / "amp.yaml").write_text(
        spec.replace("id: opamp", "id: amp").replace("eda_opamp.lib", "amp.lib"),
        encoding="utf-8",
    )
    diag = Diagnostics()
    load_design(root, diag)
    assert [e.rule for e in diag.errors] == ["unsafe_model"]
    assert ".control block" in diag.errors[0].message
    with pytest.raises(ValueError, match="control block"):
        vendor.plan_import(models / "amp.lib", "amp2", "DEMOAMP", None, None)
    assert not list(tmp_path.rglob("pwned"))


def _pico(tmp_path: Path) -> Path:
    root = tmp_path / "pico_io"
    shutil.copytree(EXAMPLES / "pico_io", root, ignore=shutil.ignore_patterns("out", "*.jsonl"))
    return root


@pytest.mark.parametrize("sources", ["['../../outside']", "['/etc']", "[]"])
def test_firmware_sources_stay_in_the_project(tmp_path: Path, sources: str) -> None:
    root = _pico(tmp_path)
    firmware = root / "design" / "firmware.yaml"
    firmware.write_text(
        firmware.read_text(encoding="utf-8") + f"sources: {sources}\n", encoding="utf-8"
    )
    result = pipeline.fw(root)
    assert result.errors[0].rule == "schema"
    assert result.errors[0].file == "design/firmware.yaml"
    assert not (tmp_path.parent / "outside").exists()


def test_firmware_warnings(tmp_path: Path) -> None:
    root = _pico(tmp_path)
    shutil.rmtree(root / "firmware")
    firmware = root / "design" / "firmware.yaml"
    text = firmware.read_text(encoding="utf-8")
    firmware.write_text(text.replace("mode: output, initial: low", "mode: output, pull: up"))
    result = pipeline.fw(root)
    assert result.ok
    assert sorted(w.rule for w in result.warnings) == ["no_firmware_sources", "pull_on_output"]


def test_placement_outside_a_fixed_board(make_project: MakeProject) -> None:
    board = "board: { width: 30mm, height: 20mm }\nplacement:\n  V1: { x: 50mm, y: 6mm }\n"
    assert _led_board(make_project, board) == [
        "placement.V1 at (50mm, 6mm) is outside the 30mm x 20mm board (corner at 0, 0)"
    ]


def test_no_tests_is_a_warning(make_project: MakeProject) -> None:
    diag = Diagnostics()
    assert load_design(make_project(tests="tests: {}\n"), diag) is not None
    assert [w.rule for w in diag.warnings] == ["no_tests"]


@pytest.mark.parametrize(
    ("text", "message"),
    [("", "is empty"), ("# only a comment\n", "is empty"), ("- a\n- b\n", "holds a list")],
)
def test_files_that_are_not_mappings(make_project: MakeProject, text: str, message: str) -> None:
    root = make_project()
    (root / "design" / "circuit.yaml").write_text(text, encoding="utf-8")
    diag = Diagnostics()
    assert load_design(root, diag) is None
    assert diag.errors[0].message.startswith(f"design/circuit.yaml {message}")


@requires_ngspice
def test_period_without_two_rising_crossings(make_project: MakeProject) -> None:
    tests = """
    tests:
      t:
        analysis: { type: tran, step: 10us, stop: 1ms }
        expect:
          - { measure: "period(voltage(VCC), 1V)", max: 1ms }
    """
    result = pipeline.sim(make_project(tests=tests))
    assert "does not rise through that level twice" in result.errors[0].message


@pytest.mark.parametrize(
    ("target", "series", "nearest"),
    [(4.7e-12, "E24", 4.7e-12), (2.2e-12, "E12", 2.2e-12), (1.1e-13, "E24", 1.1e-13)],
)
def test_standard_values_in_the_picofarad_range(target: float, series: str, nearest: float) -> None:
    assert eseries.nearest(target, series).nearest == pytest.approx(nearest, rel=1e-9)


def test_values_beyond_tera_use_an_exponent() -> None:
    assert format_quantity(1e30, "ohm") == "1e+30Ω"
    assert format_quantity(4.7e12, "ohm") == "4.7TΩ"


@pytest.mark.parametrize("argv", [["frob"], ["value"], ["sim", ".", "--bogus"]])
def test_usage_errors_are_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    result = json.loads(capsys.readouterr().out)
    assert _rules(result) == ["usage"]


def test_inspecting_a_file_without_subcircuits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("not a model\n", encoding="utf-8")
    code, result = _cli(["model", "inspect", str(other)], capsys)
    assert code == 1
    assert _rules(result) == ["no_subckt"]
