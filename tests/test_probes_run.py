"""Probes in a real run. Needs OpenFOAM: pytest -m openfoam."""

from pathlib import Path

import pytest
from test_case import EXAMPLE
from test_fans_run import _backend

from airconstruct.case import Case
from airconstruct.probes import read_probes
from airconstruct.runner import run_case

RUNS = Path(__file__).parent.parent / "runs"


@pytest.mark.openfoam
def test_probes_record_history():
    d = Case.load(EXAMPLE).model_dump(mode="json")
    d["name"] = "electronics_box_probes"
    d["precision"] = {"level": 1}
    d["solver"]["iterations"] = 3000
    d["probes"] = [
        {"name": "inlet_jet", "point": {"x": 0.03, "y": 0.06, "z": 0.03}},
        {"name": "cpu_core", "point": {"x": 0.075, "y": 0.055, "z": 0.0085}},
    ]
    case = Case.model_validate(d)
    res = run_case(case, RUNS, _backend())
    assert res.success, "\n".join(res.errors)
    p = read_probes(case, res.case_dir)
    jet, cpu = p["inlet_jet"], p["cpu_core"]
    print(f"{len(jet.iterations)} samples, jet {jet.last()}, cpu {cpu.last()}")
    assert len(jet.iterations) > 10 and jet.iterations == sorted(set(jet.iterations))
    assert jet.iterations[-1] > 0.8 * res.iterations  # samples continue across phases
    assert 0.3 < jet.last()[1] < 3.0  # ~1 m/s inlet jet
    t_min, t_max = res.region_temperatures["cpu"]
    assert t_min - 0.5 <= cpu.last()[0] <= t_max + 0.5
    assert cpu.last()[0] > jet.last()[0]
