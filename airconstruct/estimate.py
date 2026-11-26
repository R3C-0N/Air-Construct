"""Run time estimate, so the user can pick a precision from an expected duration.

Owned by: core pipeline (calibrated by "validation").

Model: a short coupled flow phase then a long frozen-flow energy phase (see
runner.run_case). Constants measured with OpenFOAM v1912 on one core of a 2025 cloud
VM, on examples/electronics_box.yaml; refine them with real runs.
"""

from __future__ import annotations

from .case import Case

SECONDS_PER_CELL_ITER_FLOW = 2.6e-6  # flow + energy iteration
SECONDS_PER_CELL_ITER_ENERGY = 0.65e-6  # frozen-flow, energy-only iteration
FLOW_ITERATIONS = 400
ENERGY_ITERATIONS = 7000
PARALLEL_EFFICIENCY = 0.8  # speed-up = cores ** PARALLEL_EFFICIENCY
MESHING_S = 3.0


def estimate_runtime_s(case: Case, n_cells: int | None = None) -> float:
    """Rough wall time [s] of run_case for this case (meshing + solve)."""
    if n_cells is None:
        from .mesh import plan_mesh

        n_cells = plan_mesh(case).n_cells
    per_cell = (FLOW_ITERATIONS * SECONDS_PER_CELL_ITER_FLOW
                + ENERGY_ITERATIONS * SECONDS_PER_CELL_ITER_ENERGY)
    solve = n_cells * per_cell / max(1, case.solver.cores) ** PARALLEL_EFFICIENCY
    return MESHING_S + n_cells * 2e-5 + solve


def format_duration(seconds: float) -> str:
    if seconds < 90:
        return f"~{round(seconds)} s"
    if seconds < 5400:
        return f"~{round(seconds / 60)} min"
    return f"~{seconds / 3600:.1f} h"
