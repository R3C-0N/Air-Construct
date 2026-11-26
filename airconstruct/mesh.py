"""Meshing: from a Case to a multi-region OpenFOAM mesh.

Owned by: workstream "meshing".

Strategy (MVP, axis-aligned boxes only):
  1. blockMesh: one hex block for the enclosure, with piecewise-uniform grading so that
     every component and opening edge falls exactly on a cell face (no staircase, thin
     parts such as boards still get cells). One "walls" patch for the 6 faces, or
     "walls_<face>" patches when the walls differ per face.
  2. topoSet + createPatch: each Opening becomes its own patch (named opening.name),
     selected with a thin boxToFace on the enclosure face.
  3. topoSet: one cellZone per component (boxToCell) + cellZone "air" for the rest.
  4. splitMeshRegions -cellZonesOnly -overwrite: one region per zone. Interfaces are
     named "<a>_to_<b>" (mappedWall), as OpenFOAM does.
  5. topoSet -region air: one cellZone "grille_<opening>" per grille, the porous layer
     just inside the opening (its inner face is a grid plane, see plan_mesh).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path

from .case import Case, Face, Opening
from .foam.dictionary import write_foam_dict

# precision level -> (base cells along the largest enclosure side, min cells per segment)
LEVELS: dict[int, tuple[int, int]] = {1: (20, 1), 2: (30, 2), 3: (45, 2), 4: (65, 3), 5: (90, 4)}

_AXIS = {"x": 0, "y": 1, "z": 2}
# for each face: (normal axis, at max side?, in-plane axes)
FACE_AXES: dict[Face, tuple[int, bool, tuple[int, int]]] = {
    Face.xmin: (0, False, (1, 2)),
    Face.xmax: (0, True, (1, 2)),
    Face.ymin: (1, False, (0, 2)),
    Face.ymax: (1, True, (0, 2)),
    Face.zmin: (2, False, (0, 1)),
    Face.zmax: (2, True, (0, 1)),
}

TOL = 1e-9


@dataclass
class AxisGrid:
    breakpoints: list[float]  # sorted, first = 0, last = L
    cells: list[int]  # cells per segment
    ratios: list[float] | None = None  # blockMesh expansion (last / first cell) per segment

    @property
    def n(self) -> int:
        return sum(self.cells)

    def _ratios(self) -> list[float]:
        return self.ratios if self.ratios is not None else [1.0] * len(self.cells)

    def spacings(self) -> list[tuple[float, float]]:
        """(first, last) cell size of every segment."""
        out = []
        for a, b, n, r in zip(self.breakpoints, self.breakpoints[1:], self.cells, self._ratios()):
            if n == 1 or abs(r - 1.0) < 1e-12:
                out.append(((b - a) / n, (b - a) / n))
                continue
            rc = r ** (1.0 / (n - 1))
            first = (b - a) * (rc - 1.0) / (rc**n - 1.0)
            out.append((first, first * r))
        return out

    def min_spacing(self) -> float:
        return min(min(s) for s in self.spacings())


@dataclass
class MeshPlan:
    cell_size: float  # target bulk cell size [m]
    divisions: tuple[int, int, int]  # blockMesh cells along x, y, z
    n_cells: int
    warnings: list[str] = field(default_factory=list)
    axes: tuple[AxisGrid, AxisGrid, AxisGrid] | None = None


def _opening_bounds(case: Case) -> list[tuple[int, float, float]]:
    """(axis, lo, hi) intervals of every opening's in-plane extent."""
    out = []
    for o in case.openings:
        _, _, (a, b) = FACE_AXES[o.face]
        for ax, c, s in ((a, o.center[0], o.size[0]), (b, o.center[1], o.size[1])):
            out.append((ax, c - s / 2, c + s / 2))
    return out


def grille_depth(case: Case, h: float) -> float:
    """Thickness of the porous layer modelling a grille: two bulk cells, at most 10 %
    of the enclosure along the opening's normal."""
    return min(2.0 * h, 0.1 * min(case.enclosure.as_tuple()))


def grille_box(case: Case, o: Opening, depth: float) -> tuple[list[float], list[float]]:
    """(min, max) corners of the porous layer behind opening o."""
    E = case.enclosure.as_tuple()
    normal, at_max, (a, b) = FACE_AXES[o.face]
    lo, hi = [0.0] * 3, [0.0] * 3
    lo[normal], hi[normal] = (E[normal] - depth, E[normal]) if at_max else (0.0, depth)
    for k, ax in enumerate((a, b)):
        lo[ax] = o.center[k] - o.size[k] / 2
        hi[ax] = o.center[k] + o.size[k] / 2
    return lo, hi


def grille_zone(o: Opening) -> str:
    return f"grille_{o.name}"


Zone = tuple[float, float, float, float]  # (lo, hi, fine cell size, transition length)


def _spacing(x: float, h: float, zones: list[Zone]) -> float:
    """Target cell size at x: the fine size inside a zone, growing linearly back to h
    over its transition length."""
    s = h
    for lo, hi, hf, m in zones:
        d = max(lo - x, 0.0, x - hi)
        if d <= 0:
            s = min(s, hf)
        elif m > 0 and d < m:
            s = min(s, hf + (h - hf) * d / m)
    return s


def _axis_grid(L: float, points: set[float], h: float, min_cells: int,
               zones: list[Zone] | None = None) -> AxisGrid:
    zones = zones or []
    for lo, hi, _, m in zones:  # zone and transition ends are grid lines too
        points = points | {lo, hi, lo - m, hi + m}
    pts = sorted(p for p in points | {0.0, L} if -TOL <= p <= L + TOL)
    bp: list[float] = []
    for p in pts:
        p = min(max(p, 0.0), L)
        if not bp or p - bp[-1] > TOL:
            bp.append(p)
    if L - bp[-1] > TOL:
        bp.append(L)
    else:
        bp[-1] = L
    cells, ratios = [], []
    for a, b in itertools.pairwise(bp):
        if not zones:
            cells.append(max(min_cells, math.ceil((b - a) / h - 1e-6)))
            ratios.append(1.0)
            continue
        # cells = integral of dx / s(x) (midpoint rule), graded from s(a) to s(b)
        k = 64
        dx = (b - a) / k
        n_ideal = sum(dx / _spacing(a + (i + 0.5) * dx, h, zones) for i in range(k))
        n = max(min_cells, math.ceil(n_ideal - 1e-6))
        eps = 1e-9 * (b - a)
        r = _spacing(b - eps, h, zones) / _spacing(a + eps, h, zones)
        cells.append(n)
        ratios.append(1.0 if n == 1 or abs(r - 1.0) < 1e-9 else r)
    return AxisGrid(bp, cells, ratios)


def refinement_zones(case: Case, h: float) -> list[list[Zone]]:
    """Per axis, the refinement zones of case.refinements."""
    comps = {c.name: c for c in case.components}
    out: list[list[Zone]] = [[], [], []]
    for r in case.refinements:
        c = comps[r.target]
        hf = min(h, r.cell_size if r.cell_size is not None else h / r.factor)
        m = r.margin if r.margin is not None else 3.0 * h
        for i in range(3):
            out[i].append((c.box.min[i], c.box.max[i], hf, m))
    return out


def plan_mesh(case: Case) -> MeshPlan:
    """Choose spacing from case.precision; every box/opening edge is a grid line."""
    E = case.enclosure.as_tuple()
    base, min_cells = LEVELS[case.precision.level]
    h = case.precision.cell_size or max(E) / base
    points: list[set[float]] = [set(), set(), set()]
    for c in case.components:
        for i in range(3):
            points[i].update((c.box.min[i], c.box.max[i]))
    for ax, lo, hi in _opening_bounds(case):
        points[ax].update((lo, hi))
    depth = grille_depth(case, h)
    for o in case.openings:
        if o.grille is not None:  # the porous layer's inner face is a grid plane
            lo, hi = grille_box(case, o, depth)
            normal = FACE_AXES[o.face][0]
            points[normal].update((lo[normal], hi[normal]))
    zones = refinement_zones(case, h)
    axes = tuple(_axis_grid(E[i], points[i], h, min_cells, zones[i]) for i in range(3))
    div = tuple(a.n for a in axes)
    n = div[0] * div[1] * div[2]
    warnings = []
    if n > 2_000_000:
        warnings.append(f"{n:,} cells: the run will be long, lower the precision level")
    for o in case.openings:
        _, _, (a, b) = FACE_AXES[o.face]
        lo = (o.center[0] - o.size[0] / 2, o.center[1] - o.size[1] / 2)
        hi = (o.center[0] + o.size[0] / 2, o.center[1] + o.size[1] / 2)
        for k, ax in enumerate((a, b)):
            if lo[k] < -TOL or hi[k] > E[ax] + TOL:
                warnings.append(f"opening {o.name!r} extends past the enclosure face")
    return MeshPlan(cell_size=h, divisions=div, n_cells=n, warnings=warnings, axes=axes)  # type: ignore[arg-type]


def _grading(g: AxisGrid) -> list:
    return [[round(b - a, 12), c, round(r, 9)]
            for a, b, c, r in zip(g.breakpoints, g.breakpoints[1:], g.cells, g._ratios())]


def write_mesh_dicts(case: Case, case_dir: Path, plan: MeshPlan | None = None) -> MeshPlan:
    """Write system/blockMeshDict, topoSetDict.openings, createPatchDict, topoSetDict.zones."""
    plan = plan or plan_mesh(case)
    assert plan.axes is not None
    case_dir = Path(case_dir)
    sysd = case_dir / "system"
    X, Y, Z = case.enclosure.as_tuple()
    verts = [(0, 0, 0), (X, 0, 0), (X, Y, 0), (0, Y, 0), (0, 0, Z), (X, 0, Z), (X, Y, Z), (0, Y, Z)]
    nx, ny, nz = plan.divisions
    grading = "simpleGrading (" + " ".join(
        "(" + " ".join(f"({lf:g} {c} {e:g})" for lf, c, e in _grading(g)) + ")" for g in plan.axes
    ) + ")"
    write_foam_dict(
        sysd / "blockMeshDict",
        {
            "scale": 1,
            "vertices": "(\n" + "\n".join(f"    ({x:g} {y:g} {z:g})" for x, y, z in verts) + "\n)",
            "blocks": f"(\n    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) {grading}\n)",
            "edges": "()",
            "boundary": _boundary(case),
            "mergePatchPairs": "()",
        },
    )

    eps = 0.25 * min(g.min_spacing() for g in plan.axes)
    E = (X, Y, Z)
    face_actions, patches = [], []
    for o in case.openings:
        normal, at_max, (a, b) = FACE_AXES[o.face]
        lo, hi = [0.0] * 3, [0.0] * 3
        pos = E[normal] if at_max else 0.0
        lo[normal], hi[normal] = pos - eps, pos + eps
        for k, ax in enumerate((a, b)):
            lo[ax] = o.center[k] - o.size[k] / 2 + eps
            hi[ax] = o.center[k] + o.size[k] / 2 - eps
        face_actions.append(_action(o.name, "faceSet", "new", "boxToFace", {"box": _box(lo, hi)}))
        patches.append(
            {"name": o.name, "patchInfo": {"type": "patch"}, "constructFrom": "set", "set": o.name}
        )
    write_foam_dict(sysd / "topoSetDict.openings", {"actions": _list(face_actions)}, obj="topoSetDict")
    write_foam_dict(sysd / "createPatchDict", {"pointSync": False, "patches": _list(patches)})

    zone_actions = []
    shrink = eps
    for i, c in enumerate(case.components):
        lo = [v + shrink for v in c.box.min]
        hi = [v - shrink for v in c.box.max]
        zone_actions.append(_action(c.name, "cellSet", "new", "boxToCell", {"box": _box(lo, hi)}))
        for prev in case.components[:i]:  # overlaps belong to the first component listed
            zone_actions.append(_action(c.name, "cellSet", "subtract", "cellToCell", {"set": prev.name}))
    air = case.fluid_region
    zone_actions.append(
        _action(air, "cellSet", "new", "boxToCell", {"box": _box([-1.0] * 3, [v + 1.0 for v in E])})
    )
    for c in case.components:
        zone_actions.append(_action(air, "cellSet", "subtract", "cellToCell", {"set": c.name}))
    for name in [air] + case.solid_regions:
        zone_actions.append(_action(name, "cellZoneSet", "new", "setToCellZone", {"set": name}))
    write_foam_dict(sysd / "topoSetDict.zones", {"actions": _list(zone_actions)}, obj="topoSetDict")

    # porous layers of the grilles: cellZones of the air region, made after the split
    depth = grille_depth(case, plan.cell_size)
    grille_actions = []
    for o in case.openings:
        if o.grille is None:
            continue
        lo, hi = grille_box(case, o, depth)
        z = grille_zone(o)
        grille_actions.append(_action(z, "cellSet", "new", "boxToCell", {"box": _box(lo, hi)}))
        grille_actions.append(_action(z, "cellZoneSet", "new", "setToCellZone", {"set": z}))
    if grille_actions:
        # topoSet -region air -dict system/X reads system/air/X (OpenFOAM v2406)
        write_foam_dict(sysd / case.fluid_region / "topoSetDict.grilles",
                        {"actions": _list(grille_actions)}, obj="topoSetDict")
    return plan


# blockMesh vertex quads of the enclosure faces
_FACE_QUADS = {Face.xmin: "(0 4 7 3)", Face.xmax: "(1 2 6 5)", Face.ymin: "(0 1 5 4)",
               Face.ymax: "(3 7 6 2)", Face.zmin: "(0 3 2 1)", Face.zmax: "(4 5 6 7)"}


def _boundary(case: Case) -> str:
    """One wall patch for the 6 faces, or one per face (see Case.wall_patches)."""
    body = ""
    for name, faces in case.wall_patches().items():
        quads = " ".join(_FACE_QUADS[f] for f in faces)
        body += (f"    {name}\n    {{\n        type wall;\n        faces\n        (\n"
                 f"            {quads}\n        );\n    }}\n")
    return "(\n" + body + ")"


def _box(lo, hi) -> str:
    return "(" + " ".join(f"{v:.9g}" for v in lo) + ") (" + " ".join(f"{v:.9g}" for v in hi) + ")"


def _action(name: str, typ: str, action: str, source: str, info: dict) -> dict:
    return {"name": name, "type": typ, "action": action, "source": source, "sourceInfo": info}


def _list(items: list[dict]) -> str:
    """A list of anonymous sub-dictionaries, as topoSet/createPatch expect."""
    from .foam.dictionary import fmt_entries

    body = "".join("    {\n" + fmt_entries(it, 2) + "    }\n" for it in items)
    return "(\n" + body + ")"


def mesh_commands(case: Case) -> list[list[str]]:
    """Ordered commands (run inside the case dir) that build the multi-region mesh."""
    cmds = [["blockMesh"]]
    if case.openings:
        cmds += [["topoSet", "-dict", "system/topoSetDict.openings"], ["createPatch", "-overwrite"]]
    cmds += [
        ["topoSet", "-dict", "system/topoSetDict.zones"],
        ["splitMeshRegions", "-cellZonesOnly", "-overwrite"],
    ]
    if any(o.grille is not None for o in case.openings):
        cmds.append(["topoSet", "-region", case.fluid_region, "-dict", "system/topoSetDict.grilles"])
    return cmds
