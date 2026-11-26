import pytest

from airconstruct.case import Case
from airconstruct.mesh import mesh_commands, plan_mesh, write_mesh_dicts

from .test_case import EXAMPLE


@pytest.fixture
def case():
    return Case.load(EXAMPLE)


def test_grid_lines_follow_component_edges(case):
    plan = plan_mesh(case)
    for c in case.components:
        for ax in range(3):
            bp = plan.axes[ax].breakpoints
            assert any(abs(b - c.box.min[ax]) < 1e-12 for b in bp)
            assert any(abs(b - c.box.max[ax]) < 1e-12 for b in bp)
    assert plan.n_cells == plan.divisions[0] * plan.divisions[1] * plan.divisions[2]


def test_precision_increases_cells(case):
    counts = []
    for level in range(1, 6):
        case.precision.level = level
        counts.append(plan_mesh(case).n_cells)
    assert counts == sorted(counts) and counts[0] < counts[-1]


def test_thin_parts_get_min_cells(case):
    case.precision.level = 2  # min 2 cells per segment
    plan = plan_mesh(case)
    z = plan.axes[2]
    board = next(c for c in case.components if c.name == "board")
    i = next(k for k, b in enumerate(z.breakpoints) if abs(b - board.box.min[2]) < 1e-12)
    assert z.cells[i] >= 2


def test_dicts_written(case, tmp_path):
    write_mesh_dicts(case, tmp_path)
    sysd = tmp_path / "system"
    bm = (sysd / "blockMeshDict").read_text()
    assert "simpleGrading" in bm and "walls" in bm
    zones = (sysd / "topoSetDict.zones").read_text()
    for name in ["air"] + case.solid_regions:
        assert f"name            {name};" in zones
    assert "constructFrom   set;" in (sysd / "createPatchDict").read_text()
    assert mesh_commands(case)[-1][0] == "splitMeshRegions"
