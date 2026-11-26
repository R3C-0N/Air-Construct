"""End-to-end run of the example case. Needs OpenFOAM: pytest -m openfoam."""

import os
from pathlib import Path

import pytest
from test_case import EXAMPLE

from airconstruct.case import Case
from airconstruct.runner import LocalBackend, run_case

RUNS = Path(__file__).parent.parent / "runs"


@pytest.mark.openfoam
def test_example_converges():
    backend = LocalBackend()
    ok, msg = backend.available()
    if not ok:
        if os.environ.get("AIRCONSTRUCT_REQUIRE_OPENFOAM"):
            pytest.fail(msg)
        pytest.skip(msg)
    case = Case.load(EXAMPLE)
    case.precision.level = 1
    res = run_case(case, RUNS, backend)
    assert res.success, "\n".join(res.errors)
    assert res.converged
    t = {r: tmax for r, (_, tmax) in res.region_temperatures.items()}
    # every powered part is hotter than the air inlet, the CPU is hotter than its heatsink
    assert all(t[c.name] > case.ambient_temperature for c in case.components if c.power > 0)
    assert t["cpu"] >= t["heatsink"]
