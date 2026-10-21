import csv
import shutil
import zipfile
from pathlib import Path

import pytest

from eda import pcb, pipeline
from eda.adapters import freerouting
from eda.design import load_design
from eda.footprints import pad_numbers
from eda.project import REPO_ROOT
from eda.result import Diagnostics
from tests.conftest import LED_CIRCUIT, MakeProject, requires_kicad, requires_ngspice

TESTS = "tests: {}\n"


def _board(
    make_project: MakeProject, board: str, circuit: str = LED_CIRCUIT, name: str = "demo"
) -> Path:
    root = make_project(circuit, TESTS, name=name)
    (root / "design" / "board.yaml").write_text(board, encoding="utf-8")
    return root


def _issues(root: Path) -> tuple[list[str], list[str]]:
    diag = Diagnostics()
    load_design(root, diag)
    return [e.rule for e in diag.errors], [w.rule for w in diag.warnings]


@pytest.mark.skipif(pad_numbers("LED_THT:LED_D5.0mm") is None, reason="needs KiCad footprints")
def test_footprint_pads() -> None:
    assert pad_numbers("LED_THT:LED_D5.0mm") == frozenset({"1", "2"})
    assert pad_numbers("Nope:Nothing") is None
    assert pad_numbers("missing-colon") is None


def test_board_resolves(make_project: MakeProject) -> None:
    root = _board(make_project, "board: {}\nplacement:\n  V1: { x: 5mm, y: 6mm }\n")
    diag = Diagnostics()
    design = load_design(root, diag)
    assert diag.ok, diag.errors
    assert design is not None and design.board is not None
    parts = {p.ref: p for p in design.board.parts}
    assert parts["D1"].pads == {"2": "N1", "1": "GND"}  # LED: pad 1 = cathode
    placement = parts["V1"].placement
    assert placement is not None and (placement.x, placement.y) == (5.0, 6.0)
    assert design.board.rules.clearance == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("board", "circuit_change", "rule"),
    [
        ("placement:\n  X9: { x: 1mm, y: 1mm }\n", None, "unknown_part"),
        ("board: { width: 30mm }\n", None, "invalid_board"),
        ("rules: { clearance: '0.2' }\n", None, "invalid_value"),
        (
            "board: {}\n",
            ("{ type: led, color: red }", "{ type: led, color: red, footprint: 'Nope:Nothing' }"),
            "unknown_footprint",
        ),
        (
            "board: {}\n",
            (
                "{ type: led, color: red }",
                "{ type: led, color: red, pads: { anode: 1, cathode: 9 } }",
            ),
            "invalid_pads",
        ),
    ],
)
def test_board_errors(
    make_project: MakeProject, board: str, circuit_change: tuple[str, str] | None, rule: str
) -> None:
    circuit = LED_CIRCUIT if circuit_change is None else LED_CIRCUIT.replace(*circuit_change)
    errors, _ = _issues(_board(make_project, board, circuit))
    assert rule in errors


def test_part_without_footprint(make_project: MakeProject, tmp_path: Path) -> None:
    root = _board(make_project, "board: {}\n")
    parts = root / "parts"
    parts.mkdir()
    (parts / "bare.yaml").write_text(
        "id: bare\nname: Bare\ncategory: passive\npins: ['1', '2']\n"
        "params: { resistance: { unit: ohm } }\n"
        "spice: { prefix: R, template: '<name> <pin:1> <pin:2> <param:resistance>' }\n"
        "symbol: { cell: r_v, ports: { '1': A, '2': B } }\n",
        encoding="utf-8",
    )
    circuit = (root / "design" / "circuit.yaml").read_text(encoding="utf-8")
    (root / "design" / "circuit.yaml").write_text(
        circuit.replace("type: resistor", "type: bare"), encoding="utf-8"
    )
    errors, _ = _issues(root)
    assert "no_footprint" in errors


@requires_ngspice
@requires_kicad
@pytest.mark.skipif(not freerouting.JAR.is_file(), reason="needs tools/freerouting/fetch.sh")
def test_pcb_end_to_end(make_project: MakeProject) -> None:
    root = _board(make_project, "board: {}\nrules: { track_width: 0.5mm }\n")
    first = pipeline.pcb(root)
    assert first.ok, first.errors
    data = first.data
    routing = data["routing"]
    assert isinstance(routing, dict)
    assert routing["unrouted"] == 0 and routing["cached"] is False
    drc = data["drc"]
    assert isinstance(drc, dict) and drc["errors"] == 0
    outputs = data["outputs"]
    assert isinstance(outputs, dict)
    archive = root / str(outputs["fabrication"])
    names = zipfile.ZipFile(archive).namelist()
    assert any(n.endswith("F_Cu.gtl") for n in names)
    assert any(n.endswith(".drl") for n in names)
    assert any(n.endswith("-bom.csv") for n in names)
    assert (root / "out" / "board.svg").is_file()

    second = pipeline.pcb(root)
    routing2 = second.data["routing"]
    assert isinstance(routing2, dict)
    assert routing2["cached"] is True
    assert routing2["track_length_mm"] == routing["track_length_mm"]


def test_pcb_without_board_file(make_project: MakeProject) -> None:
    result = pipeline.pcb(make_project())
    assert [e.rule for e in result.errors] == ["no_board"]


BOM_CIRCUIT = """
parts:
  V1: { type: dc_voltage_source, voltage: 12V }
  V2: { type: dc_voltage_source, voltage: 12V }
  U1: { type: opamp }
  U2: { type: opamp, mpn: NE5532 }
  R1: { type: resistor, resistance: 10kohm }
  R2: { type: resistor, resistance: 10kohm }
  RV1: { type: potentiometer, resistance: 10kohm, position: 25% }
nets:
  VP: [V1.+, U1.v+, U2.v+]
  VN: [V2.-, U1.v-, U2.v-]
  A: [U1.in+, U1.in-, U1.out, R1.1, RV1.1]
  B: [U2.in+, U2.in-, U2.out, R2.1, RV1.3]
  GND: [V1.-, V2.+, R1.2, R2.2, RV1.2]
"""


@pytest.mark.skipif(pad_numbers("LED_THT:LED_D5.0mm") is None, reason="needs KiCad footprints")
def test_bom_groups_parts(make_project: MakeProject, tmp_path: Path) -> None:
    root = _board(make_project, "board: {}\n", circuit=BOM_CIRCUIT)
    diag = Diagnostics()
    design = load_design(root, diag)
    assert design is not None and design.board is not None, diag.errors
    bom = pcb._write_bom(design, design.board, tmp_path / "bom.csv", diag)  # pyright: ignore[reportPrivateUsage]
    rows = {r["References"]: r for r in csv.DictReader(bom.open(encoding="utf-8"))}
    assert rows["V1 V2"]["Qty"] == "2" and rows["V1 V2"]["Part"] == "Connector"
    assert rows["R1 R2"]["Qty"] == "2" and rows["R1 R2"]["Value"] == "10kΩ"
    assert rows["RV1"]["Value"] == "10kΩ"  # the wiper position is simulation state
    assert rows["U1"]["Note"].startswith("generic model")
    assert rows["U2"]["MPN"] == "NE5532" and rows["U2"]["Note"] == ""
    generic = [w for w in diag.warnings if w.rule == "bom_generic_part"]
    assert [w.details["part"] for w in generic] == ["U1"]


def test_pour_rules(make_project: MakeProject) -> None:
    root = _board(
        make_project,
        "board: {}\npours:\n  - { net: NOPE }\n  - { net: GND, layer: both }\n"
        "  - { net: VCC, layer: top }\n  - { net: N1, min_width: 1V }\n",
    )
    errors, _ = _issues(root)
    assert sorted(errors) == ["pour_net", "pour_overlap", "pour_overlap"]
    root = _board(make_project, "pours:\n  - { net: GND, min_width: 1V }\n", name="other")
    errors, _ = _issues(root)
    assert errors == ["invalid_value"]


@requires_kicad
def test_ground_pour(make_project: MakeProject) -> None:
    if not freerouting.JAR.is_file():
        pytest.skip("Freerouting is not downloaded (tools/freerouting/fetch.sh)")
    root = _board(make_project, "board: {}\npours:\n  - { net: GND, layer: both }\n")
    result = pipeline.pcb(root)
    assert result.ok, result.errors
    pours = result.data["pours"]
    assert isinstance(pours, list) and len(pours) == 2
    for pour in pours:
        assert isinstance(pour, dict) and pour["net"] == "GND"
        area = pour["area_mm2"]
        assert isinstance(area, float) and area > 50
    drc = result.data["drc"]
    assert isinstance(drc, dict) and drc["errors"] == 0


@requires_kicad
def test_connected_placement_shortens_tracks(tmp_path: Path) -> None:
    if not freerouting.JAR.is_file():
        pytest.skip("Freerouting is not downloaded (tools/freerouting/fetch.sh)")
    # Compared on the half-perimeter wire length of the placement: deterministic, unlike
    # Freerouting, which sometimes leaves a connection open on the shelf layout.
    lengths: dict[str, float] = {}
    for placer in ("shelf", "connected"):
        root = tmp_path / placer
        shutil.copytree(
            REPO_ROOT / "projects" / "opamp_amp",
            root,
            ignore=shutil.ignore_patterns("out", "iterations.jsonl"),
        )
        board = root / "design" / "board.yaml"
        text = board.read_text(encoding="utf-8")
        board.write_text(text.replace("board:\n", f"board:\n  placer: {placer}\n", 1), "utf-8")
        result = pipeline.pcb(root)
        assert not result.tool_error, result.errors
        routing = result.data["routing"]
        assert isinstance(routing, dict)
        length = routing["hpwl_mm"]
        assert isinstance(length, float) and length > 0
        lengths[placer] = length
    assert lengths["connected"] < 0.9 * lengths["shelf"]


@requires_kicad
def test_fixed_board_too_small(make_project: MakeProject) -> None:
    root = _board(make_project, "board: { width: 8mm, height: 8mm, margin: 1mm }\n")
    result = pipeline.pcb(root)
    assert not result.tool_error  # the design's fault: exit code 1
    assert [e.rule for e in result.errors] == ["no_room"]
    assert "no room for" in result.errors[0].message
    assert result.errors[0].file == "design/board.yaml"
