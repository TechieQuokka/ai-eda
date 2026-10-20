import shutil
from pathlib import Path

import pytest

from eda import kicad_sch, pipeline, sexpr
from eda.design import load_design
from eda.result import Diagnostics
from tests.conftest import MakeProject

requires_symbols = pytest.mark.skipif(
    not (kicad_sch.SYMBOL_DIR / "Device.kicad_sym").is_file(), reason="needs KiCad symbols"
)
requires_kicad_cli = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli")

TESTS = (
    "tests:\n  t:\n    analysis: { type: op }\n    expect: [{ measure: current(D1), max: 1A }]\n"
)


def test_sexpr_round_trip() -> None:
    text = '(a (b 1 "two words") (c "q\\"uote" yes))'
    tree = sexpr.parse(text)
    assert tree == ["a", ["b", "1", "two words"], ["c", 'q"uote', "yes"]]
    assert isinstance(tree[0], sexpr.Atom) and not isinstance(sexpr.child(tree, "b")[2], sexpr.Atom)  # type: ignore[index]
    assert sexpr.parse(sexpr.dumps(tree)) == tree
    with pytest.raises(ValueError):
        sexpr.parse("(a (b)")


def test_compare_reports_differences() -> None:
    expected = {"N1": {("R1", "1"), ("D1", "2")}, "GND": {("D1", "1"), ("V1", "2")}}
    renamed = {"x": {("R1", "1"), ("D1", "2")}, "GND": {("D1", "1"), ("V1", "2")}}
    assert kicad_sch.compare(expected, renamed) == []  # names may differ, members may not
    problems = kicad_sch.compare(expected, {"N1": {("R1", "1")}, "GND": {("D1", "1"), ("V1", "2")}})
    assert len(problems) == 2


@requires_symbols
def test_led_schematic(make_project: MakeProject) -> None:
    root = make_project(tests=TESTS)
    diag = Diagnostics()
    design = load_design(root, diag)
    assert design is not None, diag.errors
    schematic = kicad_sch.build(design)
    tree = sexpr.parse(schematic.text)
    assert sexpr.head(tree) == "kicad_sch"
    libs = [str(s[1]) for s in sexpr.children(sexpr.child(tree, "lib_symbols") or [], "symbol")]
    assert {"Device:R", "Device:LED", "Simulation_SPICE:VDC", "power:GND"} <= set(libs)
    assert schematic.expected["N1"] == {("R1", "2"), ("D1", "2")}  # LED anode is pin 2
    labels = {str(item[1]) for item in sexpr.children(tree, "label")}
    assert labels == {"VCC", "N1"}  # GND is drawn with ground symbols
    assert kicad_sch.build(design).text == schematic.text  # deterministic


@requires_symbols
def test_part_without_symbol_gets_a_box(make_project: MakeProject) -> None:
    root = make_project(tests=TESTS)
    (root / "parts").mkdir()
    (root / "parts" / "sensor.yaml").write_text(
        "id: sensor\nname: Sensor\ncategory: ic\npins: [vdd, out, gnd]\n"
        "spice:\n  prefix: R\n  pin_currents: true\n"
        "  template: |\n    <name>a <pin:vdd> <pin:out> 1k\n    <name>b <pin:out> <pin:gnd> 1k\n"
        "symbol: { cell: x_sensor, ports: { vdd: vdd, out: out, gnd: gnd }, label: sensor }\n",
        encoding="utf-8",
    )
    circuit = (root / "design" / "circuit.yaml").read_text(encoding="utf-8")
    circuit = circuit.replace(
        "parts:\n", "parts:\n  U1: { type: sensor }\n  R2: { type: resistor, resistance: 1kohm }\n"
    )
    circuit = circuit.replace("VCC: [V1.+, R1.1]", "VCC: [V1.+, R1.1, U1.vdd]")
    circuit = circuit.replace("GND: [D1.cathode, V1.-]", "GND: [D1.cathode, V1.-, U1.gnd, R2.2]")
    circuit += "  SENSE: [U1.out, R2.1]\n"
    (root / "design" / "circuit.yaml").write_text(circuit, encoding="utf-8")
    diag = Diagnostics()
    design = load_design(root, diag)
    assert design is not None, diag.errors
    schematic = kicad_sch.build(design)
    assert schematic.symbols["U1"] == "eda:sensor"
    assert ("U1", "2") in schematic.expected["SENSE"]  # pins numbered in part order


@requires_symbols
@requires_kicad_cli
def test_render_checks_the_schematic_with_kicad(make_project: MakeProject) -> None:
    root = make_project(tests=TESTS)
    result = pipeline.render(root)
    assert not result.tool_error, result.errors
    kicad = result.data["kicad"]
    assert isinstance(kicad, dict)
    assert kicad["netlist_matches"] is True
    assert (root / "out" / "kicad" / "demo.kicad_sch").is_file()
    assert isinstance(kicad["svg"], str) and (root / kicad["svg"]).is_file()
    assert not [w for w in result.warnings if w.rule.startswith("kicad_erc_")]


def test_missing_symbol_library_is_a_warning(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = make_project(tests=TESTS)
    monkeypatch.setattr(kicad_sch, "SYMBOL_DIR", tmp_path / "none")
    kicad_sch._library.cache_clear()  # pyright: ignore[reportPrivateUsage]
    diag = Diagnostics()
    design = load_design(root, diag)
    assert design is not None
    assert kicad_sch.export(design, diag) == {}
    assert [w.rule for w in diag.warnings] == ["kicad_sch_skipped"]
    kicad_sch._library.cache_clear()  # pyright: ignore[reportPrivateUsage]
