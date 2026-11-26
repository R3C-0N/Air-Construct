"""Enclosure walls exchanging heat in a real run. Needs OpenFOAM: pytest -m openfoam."""

from pathlib import Path

import pytest
from test_case import EXAMPLE
from test_fans_run import _backend

from airconstruct.case import Case, Walls
from airconstruct.runner import run_case

RUNS = Path(__file__).parent.parent / "runs"


@pytest.mark.openfoam
def test_walls_exchange_heat():
    pytest.importorskip("pyvista")
    from airconstruct.results import summarize
    from airconstruct.validation import conservation_checks

    case = Case.load(EXAMPLE)
    case.name = "electronics_box_walls"
    case.precision.level = 1
    case.solver.iterations = 4000
    # steel box losing heat outside through still air, standing on an insulating table;
    # low inlet flow so the walls carry a fair share of the 13 W
    case.openings[0].flow_rate = 0.0008
    case.walls = Walls(mode="exchange", material="steel", thickness=0.001, h_external=10,
                       emissivity=0.8, faces={"zmin": {"mode": "adiabatic"}})
    res = run_case(case, RUNS, _backend())
    assert res.success, "\n".join(res.errors)
    s = summarize(case, res.case_dir)
    print(f"air {s.heat_to_air_w:.2f} W, walls {s.heat_through_walls_w:.2f} W, "
          f"power {s.total_power_w:.2f} W, warnings {s.warnings}")
    assert 0.05 * s.total_power_w < s.heat_through_walls_w < s.total_power_w
    energy = next(c for c in conservation_checks(case, s, energy_tol=0.05)
                  if c.name.startswith("energy balance"))
    assert energy.ok, f"{energy.name}: {energy.value:.3f}"
