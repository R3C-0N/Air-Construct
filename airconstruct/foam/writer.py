"""Write a complete chtMultiRegionSimpleFoam case (everything except the mesh).

Owned by: workstream "solver setup".

Layout produced in case_dir (OpenFOAM.com, steady conjugate heat transfer):
  constant/regionProperties
  constant/air/{thermophysicalProperties, g, turbulenceProperties, radiationProperties}
  constant/<solid>/{thermophysicalProperties, radiationProperties, fvOptions (if power>0)}
  system/{controlDict, fvSchemes, fvSolution, decomposeParDict}
  system/<region>/{fvSchemes, fvSolution, decomposeParDict}
  0/air/{U, p, p_rgh, T, k, omega, nut, alphat}
  0/<solid>/{T, p}
Boundary names follow mesh.py: openings by name, "walls", interfaces "<a>_to_<b>"
(matched with regex keys). Uses foam.dictionary.write_foam_dict.
"""

from __future__ import annotations

from pathlib import Path

from ..case import Case

SOLVER_APP = "chtMultiRegionSimpleFoam"


def write_case_setup(case: Case, case_dir: Path) -> None:
    """Write constant/ and system/ files (not the mesh dicts). Safe to call before meshing."""
    raise NotImplementedError


def write_initial_fields(case: Case, case_dir: Path) -> None:
    """Write 0/<region>/ fields. Must be called AFTER splitMeshRegions."""
    raise NotImplementedError
