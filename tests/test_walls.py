"""Enclosure walls exchanging heat: case model, mesh patches, OpenFOAM setup, estimate."""

import pytest
from test_case import EXAMPLE

from airconstruct.case import Case, Face, Walls, WallThermal
from airconstruct.foam.writer import write_case_setup, write_initial_fields
from airconstruct.mesh import write_mesh_dicts
from airconstruct.thermal_estimate import estimate_temperatures


def _case(**walls):
    c = Case.load(EXAMPLE)
    c.walls = Walls(**walls)
    return c


def test_default_walls_are_adiabatic(tmp_path):
    c = Case.load(EXAMPLE)
    assert c.wall_patches() == {"walls": list(Face)}
    write_case_setup(c, tmp_path)
    write_initial_fields(c, tmp_path)
    assert "externalWallHeatFluxTemperature" not in (tmp_path / "0/air/T").read_text()


def test_wall_resistance():
    w = WallThermal(mode="exchange", material="steel", thickness=0.002, h_external=10)
    assert w.resistance() == pytest.approx(0.002 / 16 + 0.1)
    with pytest.raises(ValueError):
        WallThermal(material="unobtainium")


def test_exchange_walls_setup(tmp_path):
    c = _case(mode="exchange", material="steel", thickness=0.002, h_external=8,
              t_external=30, emissivity=0.9)
    write_mesh_dicts(c, tmp_path)
    write_case_setup(c, tmp_path)
    write_initial_fields(c, tmp_path)
    bm = (tmp_path / "system/blockMeshDict").read_text()
    assert bm.count("type wall;") == 1  # same setting everywhere: one patch
    t_air = (tmp_path / "0/air/T").read_text()
    for s in ["externalWallHeatFluxTemperature", "mode            coefficient;",
              "Ta              constant 303.15;", "h               uniform 8;",
              "thicknessLayers (0.002);", "kappaLayers     (16);", "kappaMethod     fluidThermo;",
              "emissivity      0.9;"]:
        assert s in t_air, s
    t_board = (tmp_path / "0/board/T").read_text()
    assert "kappaMethod     solidThermo;" in t_board


def test_per_face_walls(tmp_path):
    c = _case(mode="exchange", faces={"zmin": {"mode": "adiabatic"}})
    assert set(c.wall_patches()) == {f"walls_{f.value}" for f in Face}
    write_mesh_dicts(c, tmp_path)
    write_case_setup(c, tmp_path)
    write_initial_fields(c, tmp_path)
    bm = (tmp_path / "system/blockMeshDict").read_text()
    assert bm.count("type wall;") == 6 and "walls_zmin" in bm
    t_air = (tmp_path / "0/air/T").read_text()
    assert t_air.count("externalWallHeatFluxTemperature") == 5
    assert "walls_zmin" not in t_air  # adiabatic floor: zeroGradient default


def test_reserved_wall_names():
    d = Case.load(EXAMPLE).model_dump(mode="json")
    d["components"][0]["name"] = "walls_xmin"
    with pytest.raises(ValueError):
        Case.model_validate(d)


def test_estimate_walls_cool_the_box():
    adiabatic = estimate_temperatures(Case.load(EXAMPLE))
    cooled = estimate_temperatures(_case(mode="exchange", h_external=50))
    assert all(cooled[n] < adiabatic[n] for n in adiabatic)
    # a sealed box (no flow) still gets a finite temperature through its walls
    c = _case(mode="exchange")
    c.openings[0].flow_rate = None
    sealed = estimate_temperatures(c)
    assert all(t > c.ambient_temperature for t in sealed.values())
    assert sealed["cpu"] > adiabatic["cpu"]


def test_wall_heat_loss_from_results(tmp_path):
    pytest.importorskip("pyvista")
    import numpy as np
    from foam_fixture import write_case
    from test_results import DIVISIONS
    from test_results import EXAMPLE as RESULTS_BOX

    from airconstruct import results

    c = Case.load(RESULTS_BOX)
    c.components[0].box.origin.z = 0.004
    for comp in c.components[1:]:
        comp.box.origin.z = 0.006
    c.walls = Walls(mode="exchange", material="aluminium", thickness=0.002, h_external=10)
    T = 308.15  # 10 K above ambient everywhere

    def uniform_U(x):
        u = np.zeros_like(x)
        u[:, 0] = 1.0
        return u

    write_case(c, tmp_path / "box", DIVISIONS, T=lambda x, reg: np.full(len(x), T),
               U=uniform_U, p=lambda x: np.full(len(x), 101325.0))
    # the boundary condition's law, from the inner wall temperatures (summarize prefers
    # the solver's own wall fluxes when the mesh files are readable)
    _, blocks = results._read_blocks(tmp_path / "box")
    X, Y, Z = c.enclosure.as_tuple()
    wall_area = 2 * (X * Y + Y * Z + X * Z) - sum(o.area() for o in c.openings)
    expected = wall_area * 10.0 / c.walls.resistance()
    assert results.wall_heat_loss(c, blocks) == pytest.approx(expected, rel=1e-6)
    assert results.wall_heat_loss(Case.load(RESULTS_BOX), blocks) == 0
