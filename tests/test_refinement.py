"""Local mesh refinement around a component (structured, graded blockMesh grid)."""

import itertools

import pytest
from test_case import EXAMPLE

from airconstruct.case import Case
from airconstruct.mesh import plan_mesh, write_mesh_dicts


def _case(**ref):
    d = Case.load(EXAMPLE).model_dump(mode="json")
    d["refinements"] = [{"target": "cpu", **ref}]
    return Case.model_validate(d)


def _sizes_over(g, lo, hi):
    """(first, last) sizes of the segments inside [lo, hi]."""
    return [s for a, b, s in zip(g.breakpoints, g.breakpoints[1:], g.spacings())
            if a >= lo - 1e-12 and b <= hi + 1e-12]


def test_unknown_target_rejected():
    with pytest.raises(ValueError):
        _case(target="nope")


def test_refined_cells_inside_the_part():
    base = Case.load(EXAMPLE)
    plan0 = plan_mesh(base)
    case = _case(factor=4)
    plan = plan_mesh(case)
    h = plan.cell_size
    cpu = next(c for c in case.components if c.name == "cpu")
    for ax in range(3):
        sizes = _sizes_over(plan.axes[ax], cpu.box.min[ax], cpu.box.max[ax])
        assert sizes and all(max(s) <= h / 4 * 1.0001 for s in sizes)
    assert plan.n_cells > plan0.n_cells


def test_transition_is_graded_back_to_bulk():
    case = _case(factor=4, margin=0.02)
    plan = plan_mesh(case)
    h = plan.cell_size
    cpu = next(c for c in case.components if c.name == "cpu")
    g = plan.axes[0]
    # cells grow from ~h/4 at the part to ~h at the end of the margin, smoothly
    before = _sizes_over(g, cpu.box.min[0] - 0.02, cpu.box.min[0])
    assert before[-1][1] == pytest.approx(h / 4, rel=0.35)
    assert before[0][0] == pytest.approx(h, rel=0.5)
    for prev, nxt in itertools.pairwise(before):
        assert nxt[0] <= prev[1] * 1.6  # no big jump between neighbouring segments
    # far from the part the bulk spacing is unchanged
    far = _sizes_over(g, 0.16, 0.2)
    assert all(max(s) <= h * 1.0001 for s in far)


def test_cell_size_overrides_factor(tmp_path):
    case = _case(cell_size=0.001, margin=0.0)
    plan = write_mesh_dicts(case, tmp_path)
    assert plan.axes[2].min_spacing() <= 0.001 + 1e-12
    bm = (tmp_path / "system/blockMeshDict").read_text()
    assert "simpleGrading" in bm


def test_no_refinement_keeps_uniform_segments():
    plan = plan_mesh(Case.load(EXAMPLE))
    assert all(r == 1.0 for g in plan.axes for r in g._ratios())
