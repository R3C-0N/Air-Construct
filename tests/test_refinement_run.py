"""Local refinement in a real run. Needs OpenFOAM: pytest -m openfoam."""

from pathlib import Path

import pytest
from test_case import EXAMPLE
from test_fans_run import _backend

from airconstruct.case import Case
from airconstruct.mesh import plan_mesh
from airconstruct.runner import run_case

RUNS = Path(__file__).parent.parent / "runs"


@pytest.mark.openfoam
def test_refined_mesh_runs():
    d = Case.load(EXAMPLE).model_dump(mode="json")
    d["name"] = "electronics_box_refined"
    d["precision"] = {"level": 1}
    d["solver"]["iterations"] = 3000
    d["refinements"] = [{"target": "cpu", "factor": 3}]
    case = Case.model_validate(d)
    res = run_case(case, RUNS, _backend())
    assert res.success, "\n".join(res.errors)
    # blockMesh made the planned number of cells, split over the regions
    log = (res.case_dir / "log.blockMesh").read_text()
    assert f"nCells: {plan_mesh(case).n_cells}" in log
    t = {r: tmax for r, (_, tmax) in res.region_temperatures.items()}
    assert t["cpu"] > case.ambient_temperature and t["cpu"] >= t["heatsink"]
