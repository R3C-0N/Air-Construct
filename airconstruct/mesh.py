"""Meshing: from a Case to a multi-region OpenFOAM mesh.

Owned by: workstream "meshing".

Strategy (MVP, axis-aligned boxes only):
  1. blockMesh: uniform hex mesh of the enclosure, cell size from Precision.
     Patches: one "walls" patch for the 6 faces.
  2. topoSet + createPatch: each Opening becomes its own patch (named opening.name),
     selected with a thin boxToFace on the enclosure face.
  3. topoSet: one cellZone per component (boxToCell) + cellZone "air" for the rest.
  4. splitMeshRegions -cellZonesOnly -overwrite: one region per zone. Interfaces are
     named "<a>_to_<b>" (mappedWall), as OpenFOAM does.

Public interface (used by foam.writer and runner):
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .case import Case


@dataclass
class MeshPlan:
    cell_size: float  # [m]
    divisions: tuple[int, int, int]  # blockMesh cells along x, y, z
    n_cells: int
    warnings: list[str]


def plan_mesh(case: Case) -> MeshPlan:
    """Choose cell size and divisions from case.precision. Must guarantee each
    component and opening spans at least ~2 cells (warn otherwise)."""
    raise NotImplementedError


def write_mesh_dicts(case: Case, case_dir: Path, plan: MeshPlan | None = None) -> MeshPlan:
    """Write system/blockMeshDict, system/topoSetDict*, system/createPatchDict.
    Returns the plan used."""
    raise NotImplementedError


def mesh_commands(case: Case) -> list[list[str]]:
    """Ordered OpenFOAM commands (argv lists, run inside case_dir) that build the
    multi-region mesh, e.g. [["blockMesh"], ["topoSet", "-dict", ...], ...]."""
    raise NotImplementedError
