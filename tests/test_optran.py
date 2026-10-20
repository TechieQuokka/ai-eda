"""Transient operating points (ngspice optran) for vendor models that do not converge."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from eda import pipeline, vendor
from eda.adapters import ngspice
from eda.design import load_design
from eda.netlist.spice import optran_command
from eda.project import REPO_ROOT
from eda.result import Diagnostics
from eda.schema.part import OptranSpec
from tests.conftest import MakeProject, requires_ngspice

TLV6001 = REPO_ROOT / "build" / "ngspice-47" / "examples" / "optran" / "models" / "TLV6001.LIB"

LOG = """\
Note: Starting dynamic gmin stepping
Warning: Dynamic gmin stepping failed
Note: Starting true gmin stepping
Warning: True gmin stepping failed
Note: Starting source stepping
Warning: source stepping failed
Note: Transient op started
Note: Transient op finished successfully
Warning: something else
"""


def test_classify_log() -> None:
    errors, warnings, via_transient = ngspice.classify_log(LOG)
    assert via_transient and errors == []
    assert warnings == ["Warning: something else"]  # the stepping noise is dropped
    failed = "Note: Transient op started\nError: Transient op failed, timestep too small\n"
    errors, _, via_transient = ngspice.classify_log(failed)
    assert not via_transient and errors == ["Error: Transient op failed, timestep too small"]


def test_optran_spec() -> None:
    assert OptranSpec(stop="1ms").seconds == pytest.approx((100e-9, 1e-3))
    with pytest.raises(ValidationError):
        OptranSpec(step="1ms", stop="1us")


INVERTING = """
description: TLV6001 inverting amplifier, gain -1000 around mid-supply.
parts:
  VCC: { type: dc_voltage_source, voltage: 5V }
  VIN: { type: dc_voltage_source, voltage: 2.5V }
  U1: { type: tlv6001_demo }
  R1: { type: resistor, resistance: 1kohm }
  R2: { type: resistor, resistance: 1Mohm }
  R3: { type: resistor, resistance: 10kohm }
  R4: { type: resistor, resistance: 10kohm }
nets:
  VP: [VCC.+, U1.v+, R3.1]
  MID: [R3.2, R4.1, U1.in+]
  IN: [VIN.+, R1.1]
  INV: [R1.2, R2.2, U1.in-]
  OUT: [U1.out, R2.1]
  GND: [VCC.-, VIN.-, R4.2, U1.v-]
"""

# The model's input offset times the gain of 1001 sets OUT: 3.38 V once settled. ngspice's
# own 10 us transient operating point stops at 0.58 V, long before that.
BIAS = """
tests:
  bias:
    analysis: { type: op }
    expect:
      - { measure: voltage(OUT), min: 3.3V, max: 3.45V }
"""


def _tlv6001_project(make_project: MakeProject, optran: str | None) -> Path:
    root = make_project(INVERTING, BIAS)
    plan = vendor.plan_import(
        TLV6001, "tlv6001_demo", "TLV6001", ["IN+", "IN-", "VCC", "VEE", "OUT"], None
    )
    vendor.write_part(plan, TLV6001, None, root / "parts", optran=optran)
    return root


@requires_ngspice
@pytest.mark.skipif(not TLV6001.is_file(), reason="needs ngspice's examples/optran models")
def test_transient_operating_point_is_settled(make_project: MakeProject) -> None:
    root = _tlv6001_project(make_project, None)
    result = pipeline.sim(root)
    assert result.ok, result.errors
    warning = next(w for w in result.warnings if w.rule == "op_via_transient")
    assert "1ms transient run" in warning.message and "settled" in warning.message
    assert warning.details["optran_stop"] == pytest.approx(1e-3)


@requires_ngspice
@pytest.mark.skipif(not TLV6001.is_file(), reason="needs ngspice's examples/optran models")
def test_part_asks_for_optran(make_project: MakeProject) -> None:
    root = _tlv6001_project(make_project, "1ms")
    diag = Diagnostics()
    design = load_design(root, diag)
    assert design is not None, diag.errors
    assert optran_command(design.circuit) == "optran 0 0 0 1e-07 0.001 0"
    result = pipeline.sim(root)
    assert result.ok, result.errors
    warning = next(w for w in result.warnings if w.rule == "op_via_transient")
    assert "as U1 asks" in warning.message
