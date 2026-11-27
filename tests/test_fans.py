"""Fans, grilles and free vents: case model, 1D estimate, OpenFOAM setup (no OpenFOAM)."""

from pathlib import Path

import pytest
from test_case import EXAMPLE

from airconstruct.case import Case, Fan, Grille
from airconstruct.fans import estimate_flow, system_loss
from airconstruct.foam.writer import opening_p0, write_case_setup, write_initial_fields
from airconstruct.mesh import grille_depth, mesh_commands, plan_mesh, write_mesh_dicts

FAN_BOX = Path(__file__).parent.parent / "examples" / "fan_box.yaml"


def _data():
    return Case.load(EXAMPLE).model_dump(mode="json")


def test_grille_loss_from_open_area():
    assert Grille(open_area=1.0).k() == pytest.approx(0.0)
    assert Grille(open_area=0.5).k() == pytest.approx(4.0, rel=1e-3)
    assert Grille(open_area=0.3).k() > Grille(open_area=0.6).k()
    assert Grille(open_area=0.3, loss_coefficient=2.5).k() == 2.5
    with pytest.raises(ValueError):
        Grille(open_area=0.0)


def test_fan_spec_validation():
    with pytest.raises(ValueError):
        Fan()
    with pytest.raises(ValueError):
        Fan(flow_rate=0.01, curve=[(0, 10), (0.01, 0)])
    with pytest.raises(ValueError):
        Fan(curve=[(0.01, 10), (0.0, 0)])  # flow rates not increasing
    with pytest.raises(ValueError):
        Fan(curve=[(0, 10), (0.01, 20)])  # pressure rising with flow
    f = Fan(curve=[(0, 20), (0.01, 10), (0.02, 0)])
    assert f.pressure_at(0.005) == pytest.approx(15)
    assert f.pressure_at(0.03) == 0 and f.pressure_at(-1) == 20


def test_opening_rules():
    d = _data()
    d["openings"][1]["flow_rate"] = 0.001  # outlet with a flow rate: use an exhaust fan
    with pytest.raises(ValueError):
        Case.model_validate(d)
    d = _data()
    d["openings"][1]["fan"] = {"flow_rate": 0.003}  # every opening imposes its flow
    with pytest.raises(ValueError):
        Case.model_validate(d)
    d = _data()
    d["openings"][0]["fan"] = {"flow_rate": 0.003}  # flow_rate and a fan
    with pytest.raises(ValueError):
        Case.model_validate(d)
    d = _data()
    del d["openings"][0]["flow_rate"]  # free inlet vent + exhaust fan: fine
    d["openings"][1]["fan"] = {"flow_rate": 0.003}
    c = Case.model_validate(d)
    assert not c.openings[0].fixed_flow and c.openings[1].fixed_flow
    assert estimate_flow(c).flow_rate == pytest.approx(0.003)


def test_curve_fan_operating_point():
    case = Case.load(FAN_BOX)
    est = estimate_flow(case)
    (fp,) = est.fans
    fan = case.openings[0].fan
    assert 0 < fp.flow_rate < fan.curve[-1][0]
    assert fp.pressure == pytest.approx(fan.pressure_at(fp.flow_rate), rel=1e-3)
    assert fp.pressure == pytest.approx(system_loss(case, fp.flow_rate), rel=1e-3)
    # a more closed grille moves the operating point to less flow
    case.openings[1].grille.open_area = 0.2
    assert estimate_flow(case).flow_rate < fp.flow_rate


def test_fan_and_grille_setup(tmp_path):
    case = Case.load(FAN_BOX)
    plan = write_mesh_dicts(case, tmp_path)
    write_case_setup(case, tmp_path)
    write_initial_fields(case, tmp_path)
    prgh = (tmp_path / "0/air/p_rgh").read_text()
    assert "fanPressure" in prgh and "direction       in;" in prgh
    assert "table ((0 25) (0.004 18) (0.008 8) (0.011 0))" in prgh
    assert f"uniform {opening_p0(case, case.openings[0]):.8g}" in prgh
    assert opening_p0(case, case.openings[0]) > case.ambient_pressure  # p_rgh at z = 0.03
    assert "prghPressure" in prgh  # the vent is a plain outlet
    u = (tmp_path / "0/air/U").read_text()
    assert u.count("pressureInletOutletVelocity") == 2
    # porous layers: one zone per grille, f = K / depth
    fvo = (tmp_path / "constant/air/fvOptions").read_text()
    depth = grille_depth(case, plan.cell_size)
    f = case.openings[1].grille.k() / depth
    assert "cellZone        grille_vent;" in fvo and f"({f:.6g} {f:.6g} {f:.6g})" in fvo
    topo = (tmp_path / "system/air/topoSetDict.grilles").read_text()
    assert "grille_fan" in topo and "grille_vent" in topo
    assert mesh_commands(case)[-1] == ["topoSet", "-region", "air", "-dict",
                                       "system/topoSetDict.grilles"]
    # the layer's inner face is a grid plane
    assert any(abs(b - depth) < 1e-12 for b in plan.axes[0].breakpoints)
    assert any(abs(b - (case.enclosure.x - depth)) < 1e-12 for b in plan.axes[0].breakpoints)
    assert plan_mesh(case).n_cells == plan.n_cells


def test_free_vent_and_exhaust_fan_setup(tmp_path):
    d = _data()
    del d["openings"][0]["flow_rate"]
    d["openings"][1]["fan"] = {"flow_rate": 0.0032}
    case = Case.model_validate(d)
    write_case_setup(case, tmp_path)
    write_initial_fields(case, tmp_path)
    prgh = (tmp_path / "0/air/p_rgh").read_text()
    assert "prghTotalPressure" in prgh
    u = (tmp_path / "0/air/U").read_text()
    assert "uniform (1 0 0)" in u  # exhaust on xmax: outwards is +x
    assert not (tmp_path / "constant/air/fvOptions").exists()  # no grille


def test_example_without_fans_unchanged(tmp_path):
    case = Case.load(EXAMPLE)
    write_case_setup(case, tmp_path)
    write_initial_fields(case, tmp_path)
    prgh = (tmp_path / "0/air/p_rgh").read_text()
    assert "prghPressure" in prgh and "fanPressure" not in prgh
    assert mesh_commands(case)[-1][0] == "splitMeshRegions"
