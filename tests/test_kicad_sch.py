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
    assert not list(sexpr.children(tree, "label"))  # N1 is a wire, VCC and GND power symbols
    assert list(sexpr.children(tree, "wire"))
    powers = {
        str(p[2])
        for s in sexpr.children(tree, "symbol")
        for p in sexpr.children(s, "property")
        if p[1] == "Value" and str(sexpr.child(s, "lib_id")[1]).startswith("power:")  # type: ignore[index]
    }
    assert powers == {"VCC", "GND", "PWR_FLAG"}
    assert schematic.unrouted == []
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


OPAMP_CIRCUIT = """
parts:
  VCC: { type: dc_voltage_source, voltage: 12V }
  VEE: { type: dc_voltage_source, voltage: 12V }
  VIN: { type: sine_voltage_source, amplitude: 0.5V, frequency: 1kHz }
  U1: { type: opamp }
  RF: { type: resistor, resistance: 10kohm }
  RG: { type: resistor, resistance: 1kohm }
  RL: { type: resistor, resistance: 10kohm }
nets:
  VP: [VCC.+, U1.v+]
  VN: [VEE.-, U1.v-]
  IN: [VIN.+, U1.in+]
  FB: [U1.in-, RF.2, RG.1]
  OUT: [U1.out, RF.1, RL.1]
  GND: [VCC.-, VEE.+, VIN.-, RG.2, RL.2]
"""
OPAMP_TESTS = (
    "tests:\n  t:\n    analysis: { type: op }\n    expect: [{ measure: voltage(OUT), max: 1V }]\n"
)


def test_symbol_geometry() -> None:
    # KiCad turns symbols counter-clockwise on the sheet (y down); mirroring flips x first
    assert kicad_sch._turn(0, 3.81, 0) == (0, -3.81)  # pyright: ignore[reportPrivateUsage]
    assert kicad_sch._turn(0, 3.81, 90) == (-3.81, 0)  # pyright: ignore[reportPrivateUsage]
    assert kicad_sch._turn(3.0, 0, 0, mirror=True) == (-3.0, 0)  # pyright: ignore[reportPrivateUsage]
    # a pin pointing into the body to the right (angle 0) leaves it to the left
    assert kicad_sch._outward(0, 0) == (-1, 0)  # pyright: ignore[reportPrivateUsage]
    assert kicad_sch._outward(270, 90) == (-1, 0)  # pyright: ignore[reportPrivateUsage]


def test_references_get_numbers_for_kicad() -> None:
    assert kicad_sch.annotate(["VCC", "RF", "RF1", "U1"]) == {
        "VCC": "VCC1",
        "RF": "RF2",
        "RF1": "RF1",
        "U1": "U1",
    }


@requires_symbols
def test_opamp_schematic_is_wired(make_project: MakeProject) -> None:
    root = make_project(circuit=OPAMP_CIRCUIT, tests=OPAMP_TESTS)
    diag = Diagnostics()
    design = load_design(root, diag)
    assert design is not None, diag.errors
    schematic = kicad_sch.build(design)
    tree = sexpr.parse(schematic.text)
    assert schematic.unrouted == []
    assert not list(sexpr.children(tree, "label"))
    assert len(list(sexpr.children(tree, "junction"))) >= 2  # FB and OUT branch
    assert schematic.references == {"VCC": "VCC1", "VEE": "VEE1", "VIN": "VIN1", "RF": "RF1",
                                    "RG": "RG1", "RL": "RL1"}  # fmt: skip
    assert ("RF1", "1") in schematic.expected["OUT"]
    x0, y0, x1, y1 = schematic.drawing
    assert 0 < x0 < x1 < 297 and 0 < y0 < y1 < 210  # on the A4 sheet
    assert schematic.paper == "A4"


@requires_symbols
@requires_kicad_cli
def test_opamp_schematic_matches_in_kicad(make_project: MakeProject) -> None:
    root = make_project(circuit=OPAMP_CIRCUIT, tests=OPAMP_TESTS)
    result = pipeline.render(root)
    assert not result.tool_error, result.errors
    kicad = result.data["kicad"]
    assert isinstance(kicad, dict)
    assert kicad["netlist_matches"] is True
    assert not [w for w in result.warnings if w.rule.startswith("kicad_erc_")]
    assert isinstance(kicad["view"], str)
    view = (root / kicad["view"]).read_text(encoding="utf-8")
    assert 'viewBox="0.0000 0.0000' not in view  # cropped to the drawing


@requires_symbols
@requires_kicad_cli
def test_unroutable_pins_get_labels(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kicad_sch._Router, "LIMIT", 0)  # pyright: ignore[reportPrivateUsage]
    root = make_project(circuit=OPAMP_CIRCUIT, tests=OPAMP_TESTS)
    result = pipeline.render(root)
    assert not result.tool_error, result.errors
    kicad = result.data["kicad"]
    assert isinstance(kicad, dict)
    assert kicad["labelled_nets"] == ["FB", "IN", "OUT"]
    assert kicad["netlist_matches"] is True  # labels on the pins connect them


def test_crop_svg(tmp_path: Path) -> None:
    svg = tmp_path / "a.svg"
    svg.write_text(
        '<svg width="297.0022mm" height="210.0072mm" viewBox="0.0000 0.0000 297.0022 210.0072">'
        "</svg>",
        encoding="utf-8",
    )
    out = kicad_sch.crop_svg(svg, tmp_path / "b.svg", (20.0, 30.0, 60.0, 50.0), margin=5.0)
    assert 'viewBox="15.0000 25.0000 50.0000 30.0000"' in out.read_text(encoding="utf-8")
    assert 'width="50.0000mm" height="30.0000mm"' in out.read_text(encoding="utf-8")


@requires_symbols
def test_ic_pins_fan_out_in_rows(make_project: MakeProject) -> None:
    leds = "".join(
        f"  R{i}: {{ type: resistor, resistance: 150ohm }}\n  D{i}: {{ type: led, color: red }}\n"
        for i in range(1, 5)
    )
    nets = "".join(
        f"  LED{i}: [U1.GP{i + 1}, R{i}.1]\n  LA{i}: [R{i}.2, D{i}.anode]\n" for i in range(1, 5)
    )
    cathodes = ", ".join(f"D{i}.cathode" for i in range(1, 5))
    circuit = (
        "parts:\n  J1: { type: dc_voltage_source, voltage: 5V }\n"
        f"  U1: {{ type: raspberry_pi_pico }}\n{leds}"
        f"nets:\n  VSYS: [J1.+, U1.VSYS]\n{nets}  GND: [J1.-, U1.GND, {cathodes}]\n"
    )
    root = make_project(circuit=circuit, tests=OPAMP_TESTS.replace("OUT", "VSYS"))
    diag = Diagnostics()
    design = load_design(root, diag)
    assert design is not None, diag.errors
    schematic = kicad_sch.build(design)
    assert schematic.unrouted == []
    tree = sexpr.parse(schematic.text)
    at = {
        str(sexpr.child(s, "property")[2]): sexpr.number(sexpr.child(s, "at")[2])  # type: ignore[index]
        for s in sexpr.children(tree, "symbol")
        if str(sexpr.child(s, "lib_id")[1]) == "Device:R"  # type: ignore[index]
    }
    assert sorted(at, key=lambda r: at[r]) == ["R1", "R2", "R3", "R4"]  # in GPIO order
    assert not list(sexpr.children(tree, "junction"))  # each row is one unbranched wire
