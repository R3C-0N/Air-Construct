"""Validation run: heated blocks in a channel against analytic values.
Needs OpenFOAM: pytest -m openfoam. The report is written to
runs/heated_blocks/validation.md (uploaded by CI with the simulation outputs)."""

import os
from pathlib import Path

import pytest

from airconstruct.case import Case
from airconstruct.runner import LocalBackend, run_case

ROOT = Path(__file__).parent.parent
CASE = ROOT / "validation" / "heated_blocks" / "case.yaml"
RUNS = ROOT / "runs"


@pytest.mark.openfoam
def test_heated_blocks_validation():
    pytest.importorskip("pyvista")
    from airconstruct.results import summarize
    from airconstruct.validation import format_report, run_checks, write_report

    backend = LocalBackend()
    ok, msg = backend.available()
    if not ok:
        if os.environ.get("AIRCONSTRUCT_REQUIRE_OPENFOAM"):
            pytest.fail(msg)
        pytest.skip(msg)
    case = Case.load(CASE)
    res = run_case(case, RUNS, backend)
    assert res.success, "\n".join(res.errors)
    summary = summarize(case, res.case_dir)
    checks = run_checks(case, summary)
    write_report(case, summary, checks, res.case_dir / "validation.md")
    print(format_report(case, summary, checks))
    print(f"{res.iterations} iterations, {res.wall_time_s:.0f} s, converged={res.converged}")
    assert res.converged, f"not converged after {res.iterations} iterations"
    failed = [f"{c.name}: {c.value:.4g} vs {c.reference:.4g} ± {c.tolerance:.3g}"
              for c in checks if not c.ok]
    assert not failed, "\n".join(failed)
