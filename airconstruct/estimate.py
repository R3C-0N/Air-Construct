"""Run time estimate, so the user can pick a precision from an expected duration.

Owned by: workstream "meshing" (calibrated by "validation").
"""

from __future__ import annotations

from .case import Case

# seconds per (cell x iteration) on one core; calibrated on CI, refine with real runs
SECONDS_PER_CELL_ITER = 4e-6


def estimate_runtime_s(case: Case, n_cells: int | None = None) -> float:
    raise NotImplementedError
