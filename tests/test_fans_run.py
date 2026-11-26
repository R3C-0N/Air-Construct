"""Fans, grilles and free vents in a real run. Needs OpenFOAM: pytest -m openfoam."""

import os
from pathlib import Path

import pytest
from test_fans import FAN_BOX

from airconstruct.case import Case, Fan
from airconstruct.fans import estimate_flow
from airconstruct.runner import LocalBackend, run_case

RUNS = Path(__file__).parent.parent / "runs"


def _backend():
    backend = LocalBackend()
    ok, msg = backend.available()
    if not ok:
        if os.environ.get("AIRCONSTRUCT_REQUIRE_OPENFOAM"):
            pytest.fail(msg)
        pytest.skip(msg)
    return backend


def _run(case):
    pytest.importorskip("pyvista")
    from airconstruct.results import summarize

    res = run_case(case, RUNS, _backend())
    assert res.success, "\n".join(res.errors)
    s = summarize(case, res.case_dir)
    print(s.fans, s.mass_flow_kg_s, s.mass_imbalance, s.warnings)
    return res, s


@pytest.mark.openfoam
def test_fan_curve_operating_point():
    case = Case.load(FAN_BOX)
    case.precision.level = 1
    case.solver.iterations = 3000
    res, s = _run(case)
    (fan,) = s.fans
    curve = case.openings[0].fan
    assert 0 < fan.flow_rate_m3_s < curve.curve[-1][0], "the fan must run inside its curve"
    assert fan.pressure_rise_pa == pytest.approx(curve.pressure_at(fan.flow_rate_m3_s))
    assert abs(s.mass_imbalance) < 0.02
    # the 1D estimate (grille and port losses only) is in the same range
    est = estimate_flow(case).fans[0].flow_rate
    assert 0.5 * est < fan.flow_rate_m3_s < 1.5 * est
    assert res.region_temperatures["chip"][1] > case.ambient_temperature


@pytest.mark.openfoam
def test_free_vent_with_exhaust_fan():
    case = Case.load(FAN_BOX)
    case.name = "fan_box_exhaust"
    case.precision.level = 1
    case.solver.iterations = 3000
    case.openings[0].fan = None  # free inlet vent behind its grille
    case.openings[1].fan = Fan(flow_rate=0.004)  # exhaust fan with a known flow rate
    case = Case.model_validate(case.model_dump())
    _, s = _run(case)
    (fan,) = s.fans
    assert fan.direction == "out"
    assert fan.flow_rate_m3_s == pytest.approx(0.004, rel=0.03)
    assert fan.pressure_rise_pa > 0  # the fan has to pull air through two grilles
    assert abs(s.mass_imbalance) < 0.02
