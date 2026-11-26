import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyvista")

from foam_fixture import write_case

from airconstruct import results
from airconstruct.case import Case

# frozen copy of the first example: the synthetic fixture mesh is built for this geometry
EXAMPLE = Path(__file__).parent / "data" / "results_box.yaml"
# cell size 5 x 5 x 2 mm: every component and opening of the example falls on cell faces
DIVISIONS = (40, 24, 30)
L = 0.2  # enclosure length along x
T_IN, DT = 298.15, 10.0  # air: 25 degC at the inlet, +10 K at the outlet
SOLID_T = {"board": 310.0, "cpu": 350.0, "regulator": 330.0, "ram": 320.0}


def air_T(x):
    return T_IN + DT * x[:, 0] / L


def T_field(x, region):
    if region == "air":
        return air_T(x)
    if region == "cpu":  # 350 K + up to 5 K along x
        return SOLID_T["cpu"] + 5.0 * (x[:, 0] - 0.06) / 0.03
    return np.full(len(x), SOLID_T[region])


def U_field(x):
    u = np.zeros_like(x)
    u[:, 0] = 1.0
    u[:, 2] = 0.5 * (x[:, 2] > 0.04)  # a faster region near the top: |U| max = sqrt(1.25)
    return u


def p_field(x):
    return 101325.0 + 10.0 * (1.0 - x[:, 0] / L)


@pytest.fixture(scope="module")
def case():
    c = Case.load(EXAMPLE)
    # put the parts on z cell faces: board 4..6 mm, chips sitting on it from 6 mm
    c.components[0].box.origin.z = 0.004
    for comp in c.components[1:]:
        comp.box.origin.z = 0.006
    return c


@pytest.fixture(scope="module")
def case_dir(case, tmp_path_factory):
    d = tmp_path_factory.mktemp("synthetic") / "electronics_box"
    write_case(case, d, DIVISIONS, T=T_field, U=U_field, p=p_field, times=("0", "100"))
    return d


@pytest.fixture(scope="module")
def summary(case, case_dir):
    return results.summarize(case, case_dir)


def test_load_regions(case_dir):
    regions = results.load_regions(case_dir)
    assert set(regions) == {"air", "board", "cpu", "regulator", "ram"}
    # cpu is 30 x 30 x 6 mm -> 6 x 6 x 3 cells
    assert regions["cpu"].n_cells == 6 * 6 * 3
    assert sum(g.n_cells for g in regions.values()) == np.prod(DIVISIONS)
    assert "T" in regions["air"].cell_data and "U" in regions["air"].cell_data


def test_load_patches_has_interfaces(case_dir):
    patches = results.load_patches(case_dir, "air")
    assert {"inlet", "outlet", "walls", "air_to_cpu", "air_to_board"} <= set(patches)
    assert "cpu_to_air" in results.load_patches(case_dir, "cpu")


def test_summary_uses_latest_time(summary):
    assert summary.time == 100.0


def test_summary_components(summary):
    by = {c.name: c for c in summary.components}
    assert [c.name for c in summary.components] == ["board", "cpu", "regulator", "ram"]
    assert by["cpu"].power_w == 15.0
    # cell centres of the cpu span x = 62.5 .. 87.5 mm
    assert by["cpu"].t_max_c == pytest.approx(350 + 5 * (0.0875 - 0.06) / 0.03 - 273.15)
    assert by["cpu"].t_mean_c == pytest.approx(350 + 2.5 - 273.15)
    assert by["ram"].t_max_c == pytest.approx(320 - 273.15)
    assert by["ram"].t_mean_c == pytest.approx(320 - 273.15)
    assert summary.hottest().name == "cpu"


def test_summary_air(summary):
    assert summary.air_t_max_c == pytest.approx(T_IN + DT * 0.1975 / L - 273.15)
    assert summary.air_v_max == pytest.approx(math.sqrt(1.25))
    assert summary.inlet_t_mean_c == pytest.approx(25.0)
    assert summary.outlet_t_mean_c == pytest.approx(35.0)


def test_pressure_drop_from_p_rgh(summary):
    # inlet and outlet are at the same heights, so p_rgh and p give the same 10 Pa
    assert summary.pressure_field == "p_rgh"
    assert summary.pressure_drop_pa == pytest.approx(10.0, abs=1e-6)


def test_energy_balance(summary):
    # openings are 80 x 40 mm, U normal = 1 m/s; the inlet flow drives the heat balance
    rho_in = 101335.0 * 28.96e-3 / (results.R_GAS * T_IN)
    rho_out = 101325.0 * 28.96e-3 / (results.R_GAS * (T_IN + DT))
    mdot = rho_in * 1.0 * 0.08 * 0.04
    assert summary.mass_flow_kg_s == pytest.approx(mdot, rel=1e-6)
    # the synthetic U is uniform, so the warmer, lighter outlet air carries less mass
    assert summary.mass_imbalance == pytest.approx(rho_out / rho_in - 1, rel=1e-6)
    assert any("mass balance" in w for w in summary.warnings)
    assert summary.heat_to_air_w == pytest.approx(mdot * 1005.0 * DT, rel=1e-6)
    assert summary.total_power_w == 23.0
    assert summary.energy_balance_error == pytest.approx(mdot * 1005.0 * DT / 23.0 - 1, rel=1e-6)


def test_missing_component_region(case, case_dir, tmp_path):
    d = tmp_path / "c"
    shutil.copytree(case_dir, d)
    shutil.rmtree(d / "constant" / "ram")
    for t in ("0", "100"):
        shutil.rmtree(d / t / "ram")
    s = results.summarize(case, d)
    ram = next(c for c in s.components if c.name == "ram")
    assert math.isnan(ram.t_max_c)
    assert any("ram" in w for w in s.warnings)


def test_pressure_falls_back_to_p(case, case_dir, tmp_path):
    d = tmp_path / "c"
    shutil.copytree(case_dir, d)
    for t in ("0", "100"):
        (d / t / "air" / "p_rgh").unlink()
    s = results.summarize(case, d)
    assert s.pressure_field == "p"
    assert s.pressure_drop_pa == pytest.approx(10.0, abs=1e-6)


def test_not_a_case(tmp_path):
    with pytest.raises(FileNotFoundError):
        results.load_regions(tmp_path)


def test_render_images_2d(case, case_dir, tmp_path):
    imgs = results.render_images(case, case_dir, tmp_path, three_d=False)
    names = {p.name for p in imgs}
    assert {"temperature_slice_z.png", "temperature_slice_y.png", "velocity_slice_z.png",
            "velocity_slice_y.png", "component_temperatures.png"} == names
    for p in imgs:
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert p.stat().st_size > 5000


@pytest.mark.skipif(not results.rendering_available(), reason="no OpenGL context")
def test_render_images_3d(case, case_dir, tmp_path):
    imgs = results.render_images(case, case_dir, tmp_path, three_d=True)
    names = {p.name for p in imgs}
    assert {"components_3d.png", "streamlines_3d.png"} <= names


def test_write_report(case, case_dir, summary, tmp_path):
    imgs = results.render_images(case, case_dir, tmp_path, three_d=False, summary=summary)
    path = results.write_report(case, case_dir, tmp_path, images=imgs)
    text = path.read_text(encoding="utf-8")
    assert path.name == "report.html"
    for name in ("cpu", "regulator", "ram", "board", "Pressure drop", "10.00 Pa"):
        assert name in text
    assert text.count("data:image/png;base64,") == len(imgs)
    data = json.loads((tmp_path / "summary.json").read_text())
    assert data["pressure_drop_pa"] == pytest.approx(10.0, abs=1e-6)
    assert data["components"][1]["name"] == "cpu"


# --------------------------------------------------------------------------- OpenFOAM

BLOCKMESH = """FoamFile {{ version 2.0; format ascii; class dictionary; object blockMeshDict; }}
convertToMeters 1;
vertices ((0 0 0) ({x} 0 0) ({x} {y} 0) (0 {y} 0) (0 0 {z}) ({x} 0 {z}) ({x} {y} {z}) (0 {y} {z}));
blocks (hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1));
boundary
(
    inlet {{ type patch; faces ((0 4 7 3)); }}
    outlet {{ type patch; faces ((1 2 6 5)); }}
    walls {{ type wall; faces ((0 1 5 4) (3 7 6 2) (0 3 2 1) (4 5 6 7)); }}
);
"""

TOPOSET = """FoamFile {{ version 2.0; format ascii; class dictionary; object topoSetDict; }}
actions
(
    {{ name cpu; type cellSet; action new; source boxToCell; box ({lo}) ({hi}); }}
    {{ name cpu; type cellZoneSet; action new; source setToCellZone; set cpu; }}
    {{ name air; type cellSet; action new; source cellToCell; set cpu; }}
    {{ name air; type cellSet; action invert; }}
    {{ name air; type cellZoneSet; action new; source setToCellZone; set air; }}
);
"""

CONTROL = """FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }
application chtMultiRegionFoam; startFrom latestTime; startTime 0; stopAt endTime;
endTime 1; deltaT 1; writeControl timeStep; writeInterval 1;
"""

FV_SCHEMES = """FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }
ddtSchemes { default steadyState; } gradSchemes { default Gauss linear; }
divSchemes { default none; } laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; } snGradSchemes { default corrected; }
"""

FV_SOLUTION = """FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }
solvers {}
"""


def _patch_names(boundary: Path) -> list[str]:
    import re

    text = boundary.read_text()
    body = text[text.index("(", text.index("}")) + 1:]
    return re.findall(r"^\s*([A-Za-z_][\w]*)\s*\n\s*\{", body, flags=re.MULTILINE)


def _field(path: Path, cls: str, internal: str, patches: dict[str, str], default: str):
    bf = "\n".join(
        f"    {n} {{ type calculated; value uniform {patches.get(n, default)}; }}"
        for n in _patch_names(path.parents[2] / "constant" / path.parent.name / "polyMesh"
                              / "boundary"))
    path.parent.mkdir(parents=True, exist_ok=True)
    dims = "[0 0 0 1 0 0 0]" if path.name == "T" else (
        "[0 1 -1 0 0 0 0]" if path.name == "U" else "[1 -1 -2 0 0 0 0]")
    path.write_text(
        f"FoamFile {{ version 2.0; format ascii; class {cls}; object {path.name}; }}\n"
        f"dimensions {dims};\ninternalField uniform {internal};\nboundaryField\n{{\n{bf}\n}}\n")


@pytest.mark.openfoam
def test_real_openfoam_mesh(tmp_path):
    """Mesh with blockMesh/topoSet/splitMeshRegions, so the reader sees real
    OpenFOAM files (mappedWall interfaces, region layout), then check the numbers."""
    if shutil.which("blockMesh") is None:
        pytest.skip("OpenFOAM is not on PATH")
    case = Case.model_validate({
        "name": "box", "enclosure": {"x": 0.1, "y": 0.05, "z": 0.05},
        "components": [{"name": "cpu", "power": 2.0, "material": "silicon",
                        "box": {"origin": {"x": 0.04, "y": 0.02, "z": 0.0},
                                "size": {"x": 0.02, "y": 0.01, "z": 0.01}}}],
        "openings": [
            {"name": "inlet", "kind": "inlet", "face": "xmin", "center": [0.025, 0.025],
             "size": [0.05, 0.05], "velocity": 1.0},
            {"name": "outlet", "kind": "outlet", "face": "xmax", "center": [0.025, 0.025],
             "size": [0.05, 0.05]}]})
    d = tmp_path / "box"
    (d / "system").mkdir(parents=True)
    (d / "system" / "controlDict").write_text(CONTROL)
    (d / "system" / "fvSchemes").write_text(FV_SCHEMES)
    (d / "system" / "fvSolution").write_text(FV_SOLUTION)
    (d / "system" / "blockMeshDict").write_text(
        BLOCKMESH.format(x=0.1, y=0.05, z=0.05, nx=20, ny=10, nz=10))
    (d / "system" / "topoSetDict").write_text(
        TOPOSET.format(lo="0.04 0.02 0", hi="0.06 0.03 0.01"))
    for cmd in (["blockMesh"], ["topoSet"], ["splitMeshRegions", "-cellZonesOnly", "-overwrite"]):
        r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert (d / "constant" / "cpu" / "polyMesh" / "boundary").exists()

    t = d / "1"
    _field(t / "air" / "T", "volScalarField", "300", {"outlet": "310", "inlet": "298.15"}, "300")
    _field(t / "air" / "U", "volVectorField", "(1 0 0)", {"walls": "(0 0 0)"}, "(1 0 0)")
    _field(t / "air" / "p", "volScalarField", "101325", {"inlet": "101330"}, "101325")
    _field(t / "air" / "p_rgh", "volScalarField", "101325", {"inlet": "101327"}, "101325")
    _field(t / "cpu" / "T", "volScalarField", "345", {}, "345")

    regions = results.load_regions(d)
    assert set(regions) == {"air", "cpu"}
    assert regions["cpu"].n_cells == 4 * 2 * 2
    assert {"air_to_cpu", "inlet", "outlet"} <= set(results.load_patches(d))
    s = results.summarize(case, d)
    assert s.time == 1.0
    assert s.components[0].t_max_c == pytest.approx(345 - 273.15)
    assert s.pressure_drop_pa == pytest.approx(2.0)
    assert s.outlet_t_mean_c == pytest.approx(310 - 273.15)
    assert s.air_v_max == pytest.approx(1.0)
    out = results.write_report(case, d, tmp_path / "post")
    assert out.exists()
    shutil.copytree(tmp_path / "post", Path("runs") / "results_real_mesh", dirs_exist_ok=True)
