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
    _check_live_monitors(case, res)
    failed = [f"{c.name}: {c.value:.4g} vs {c.reference:.4g} ± {c.tolerance:.3g}"
              for c in checks if not c.ok]
    assert not failed, "\n".join(failed)


def _check_live_monitors(case, res):
    """The live snapshots (monitor.jsonl) agree with the final results."""
    import math

    from airconstruct.monitor import read_history, residual_series

    hist = read_history(res.case_dir)
    assert len(hist) >= res.iterations * 0.9
    assert {"U", "p_rgh", "h"} <= {k.split("@")[0].rstrip("xyz") for k in residual_series(hist)}
    last = hist[-1]
    assert {c.name for c in case.components} <= set(last.temperatures)
    for name, (_, tmax) in res.region_temperatures.items():
        assert last.temperatures[name]["max_c"] == tmax
    if "T_" not in (res.case_dir / "system" / "monitors").read_text():
        return  # this OpenFOAM build cannot run function objects: min/max only
    log = (res.case_dir / "log.chtMultiRegionSimpleFoam").read_text()
    assert "heat_air write:" in log, "function objects did not print their results"
    lines = log.splitlines()
    i = lines.index(next(ln for ln in lines if "heat_air write:" in ln))
    print("function object output:\n" + "\n".join(lines[max(0, i - 30):i + 12]))
    for c in case.components:
        t = last.temperatures[c.name]
        assert t["min_c"] <= t["mean_c"] <= t["max_c"], (c.name, t)
    heat = last.part_heat_w()
    power = sum(c.power for c in case.components)
    assert sum(heat.values()) == pytest.approx(power, rel=0.05), heat
    assert last.heat_out_w == pytest.approx(power, rel=0.05)
    assert math.isfinite(last.pressure_drop_pa) and last.pressure_drop_pa > 0
