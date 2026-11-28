"""An imported CAD shape in a real run. Needs OpenFOAM: pytest -m openfoam."""

import re
from pathlib import Path

import pytest
from cad_shapes import sphere_triangles
from test_case import EXAMPLE
from test_fans_run import _backend

from airconstruct import cad
from airconstruct.case import Case
from airconstruct.mesh import plan_mesh, shape_cells
from airconstruct.runner import run_case

RUNS = Path(__file__).parent.parent / "runs"


@pytest.mark.openfoam
def test_shaped_part_runs():
    """The heat sink becomes an ellipsoid fitted in its box: its region has exactly
    the cells the voxelization predicts (blockMesh numbering is the one assumed)."""
    d = Case.load(EXAMPLE).model_dump(mode="json")
    d["name"] = "electronics_box_cad"
    d["precision"] = {"level": 2}
    d["solver"]["iterations"] = 2000
    hs = next(c for c in d["components"] if c["name"] == "heatsink")
    hs["shape"] = {"data": cad.encode(sphere_triangles((0, 0, 0), 1.0, 24)), "source": "t.stl"}
    case = Case.model_validate(d)
    expected = len(shape_cells(plan_mesh(case), next(c for c in case.components
                                                     if c.name == "heatsink")))
    res = run_case(case, RUNS, _backend())
    assert res.success, "\n".join(res.errors)
    owner = (res.case_dir / "constant" / "heatsink" / "polyMesh" / "owner").read_text()
    n = int(re.search(r"nCells:\s*(\d+)", owner).group(1))
    assert n == expected
    t = {r: tmax for r, (_, tmax) in res.region_temperatures.items()}
    assert t["cpu"] > case.ambient_temperature
