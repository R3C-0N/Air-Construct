"""Mesh facts for the Volume d'air tab, without Qt: what each precision costs, what a
local refinement adds, the mesh checks and their fixes, and the real grid for the
preview.

Owned by: thread "Volume d'air" (see docs/ui-architecture.md).

Everything works on the project data dict (``Case.model_dump(mode="json")`` shape)
and on :func:`airconstruct.mesh.plan_mesh`, so the numbers shown are the ones the
mesher will use.
"""

from __future__ import annotations

import copy
import itertools
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

from ....case import Case, OpeningKind
from ....mesh import FACE_AXES, LEVELS, TOL, AxisGrid, MeshPlan, plan_mesh

LEVEL_NAMES = {1: "Très grossier", 2: "Grossier", 3: "Moyen", 4: "Fin", 5: "Très fin"}
AXIS_NAMES = "xyz"

# fewer cells than this across a part: its temperature is poorly resolved
MIN_CELLS_ACROSS = 3
# a heat source needs at least this many cells across (passive parts may have fewer)
MIN_CELLS_ACROSS_PASSIVE = 2
MANY_CELLS = 2_000_000  # same threshold as plan_mesh's warning

Estimator = Callable[[Case], float]  # seconds


# ---- planning, cached on the physics content of the case ----------------------
def _key(case: Case) -> str:
    return json.dumps(case.model_dump(mode="json"), sort_keys=True)


@lru_cache(maxsize=64)
def _plan_from_key(key: str) -> MeshPlan:
    return plan_mesh(Case(**json.loads(key)))


def plan(case: Case) -> MeshPlan:
    """plan_mesh(case), cached (the tab asks for the same plans many times)."""
    return _plan_from_key(_key(case))


def with_changes(case: Case, fn: Callable[[dict], object]) -> Case:
    """A copy of ``case`` changed by ``fn(data)``."""
    data = case.model_dump(mode="json")
    fn(data)
    return Case(**data)


def at_level(case: Case, level: int) -> Case:
    def f(d):
        d["precision"] = {"level": level, "cell_size": None}
    return with_changes(case, f)


def bulk_cell_size(case: Case, level: int | None = None) -> float:
    """Bulk cell size [m] at ``level`` (default: the case's own precision)."""
    if level is None and case.precision.cell_size:
        return case.precision.cell_size
    base, _ = LEVELS[level or case.precision.level]
    return max(case.enclosure.as_tuple()) / base


# ---- fineness table ---------------------------------------------------------------
@dataclass(frozen=True)
class LevelRow:
    level: int
    name: str
    cells: int
    seconds: float
    cell_size: float  # bulk [m]


def level_rows(case: Case, estimate: Estimator) -> list[LevelRow]:
    """One row per precision level, refinements included."""
    rows = []
    for level in sorted(LEVELS):
        c = at_level(case, level)
        p = plan(c)
        rows.append(LevelRow(level, LEVEL_NAMES[level], p.n_cells, estimate(c), p.cell_size))
    return rows


def cell_size_range(case: Case) -> tuple[float, float]:
    """(min, max) bulk cell size [m] offered by the expert slider: a bit beyond the
    finest and the coarsest levels."""
    L = max(case.enclosure.as_tuple())
    return (L / (LEVELS[5][0] * 1.5), L / (LEVELS[1][0] * 0.75))


# ---- local refinement -------------------------------------------------------------
@dataclass(frozen=True)
class Cost:
    cells: int
    seconds: float


def refinement_cost(case: Case, target: str, estimate: Estimator) -> Cost:
    """What the refinement around ``target`` adds, compared with the same case
    without it."""
    without = with_changes(case, lambda d: d.__setitem__(
        "refinements", [r for r in d.get("refinements") or [] if r["target"] != target]))
    return Cost(plan(case).n_cells - plan(without).n_cells,
                estimate(case) - estimate(without))


def default_refinement(target: str) -> dict:
    return {"target": target, "factor": 2, "cell_size": None, "margin": None}


# ---- cells across a part ----------------------------------------------------------
def cells_between(g: AxisGrid, lo: float, hi: float) -> int:
    """Cells of the grid between lo and hi (both are grid lines for a part)."""
    n = 0
    for a, b, c in zip(g.breakpoints, g.breakpoints[1:], g.cells):
        if a >= lo - TOL and b <= hi + TOL:
            n += c
    return n


def cells_across(case: Case, name: str, p: MeshPlan | None = None) -> tuple[int, int]:
    """(cells, axis) along the part's thinnest direction in cells."""
    p = p or plan(case)
    comp = next(c for c in case.components if c.name == name)
    counts = [cells_between(p.axes[i], comp.box.min[i], comp.box.max[i]) for i in range(3)]
    axis = min(range(3), key=lambda i: counts[i])
    return counts[axis], axis


def fix_thin(case: Case, name: str, want: int = MIN_CELLS_ACROSS) -> dict:
    """The refinement (dict) that gives part ``name`` at least ``want`` cells across:
    the smallest factor that does it, else an explicit cell size."""
    existing = next((r for r in case.refinements if r.target == name), None)
    base = existing.model_dump(mode="json") if existing else default_refinement(name)

    def trial(ref: dict) -> int:
        def f(d):
            d["refinements"] = [r for r in d.get("refinements") or []
                                if r["target"] != name] + [ref]
        return cells_across(with_changes(case, f), name)[0]

    start = (existing.factor + 1) if existing and existing.cell_size is None else 2
    for factor in range(max(2, start), 9):
        ref = {**base, "factor": factor, "cell_size": None}
        if trial(ref) >= want:
            return ref
    comp = next(c for c in case.components if c.name == name)
    thickness = min(comp.box.size.as_tuple())
    return {**base, "cell_size": thickness / want}


# ---- checks -----------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    """One line of the "Contrôles" card. ``ok`` lines are shown with a tick;
    the others become alerts (``level``)."""

    ok: bool
    title: str
    detail: str = ""
    level: str = "warning"  # "error" | "warning" when not ok
    target: str | None = None  # selection key
    fix: str | None = None  # label of the fix button, None = nothing automatic
    fix_id: str = ""  # what apply_fix does
    fix_cost: str = ""  # e.g. "+6 k mailles, +2 min"
    extra: dict = field(default_factory=dict, compare=False, hash=False)


def _segment_solid(case: Case, p: MeshPlan) -> tuple[list[list[float]], set[tuple[int, int, int]]]:
    """Segment-level grid (between breakpoints) and the set of solid segment cells.
    Every part edge is a breakpoint, so a segment cell is all solid or all air.
    Parts with an imported CAD shape are left out: their box is not all solid (a
    finned heat sink, a duct), so they never count as blocking the air."""
    bps = [g.breakpoints for g in p.axes]
    mids = [[(a + b) / 2 for a, b in itertools.pairwise(bp)] for bp in bps]
    solid = set()
    for c in case.components:
        if c.shape is not None:
            continue
        lo, hi = c.box.min, c.box.max
        rng = [[k for k, m in enumerate(mids[i]) if lo[i] < m < hi[i]] for i in range(3)]
        for i in rng[0]:
            for j in rng[1]:
                for k in rng[2]:
                    solid.add((i, j, k))
    return mids, solid


def _opening_cells(case: Case, mids, opening) -> list[tuple[int, int, int]]:
    normal, at_max, (a, b) = FACE_AXES[opening.face]
    n = [len(m) for m in mids]
    out = []
    lo = (opening.center[0] - opening.size[0] / 2, opening.center[1] - opening.size[1] / 2)
    hi = (opening.center[0] + opening.size[0] / 2, opening.center[1] + opening.size[1] / 2)
    ia = [k for k, m in enumerate(mids[a]) if lo[0] < m < hi[0]]
    ib = [k for k, m in enumerate(mids[b]) if lo[1] < m < hi[1]]
    layer = n[normal] - 1 if at_max else 0
    for i in ia:
        for j in ib:
            idx = [0, 0, 0]
            idx[normal], idx[a], idx[b] = layer, i, j
            out.append(tuple(idx))
    return out


def air_paths(case: Case, p: MeshPlan | None = None) -> dict:
    """Connectivity of the air: which openings are blocked by a part, and whether
    each inlet reaches an outlet."""
    p = p or plan(case)
    mids, solid = _segment_solid(case, p)
    n = [len(m) for m in mids]
    cells = {o.name: [c for c in _opening_cells(case, mids, o) if c not in solid]
             for o in case.openings}
    blocked = [name for name, cs in cells.items() if not cs]
    # flood fill the air from every open inlet
    seen: set = set()
    q: deque = deque()
    for o in case.openings:
        if o.kind == OpeningKind.inlet:
            for c in cells[o.name]:
                if c not in seen:
                    seen.add(c)
                    q.append(c)
    while q:
        i, j, k = q.popleft()
        for di, dj, dk in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            c = (i + di, j + dj, k + dk)
            if (0 <= c[0] < n[0] and 0 <= c[1] < n[1] and 0 <= c[2] < n[2]
                    and c not in seen and c not in solid):
                seen.add(c)
                q.append(c)
    reached = [o.name for o in case.openings
               if o.kind == OpeningKind.outlet and any(c in seen for c in cells[o.name])]
    return {"blocked": blocked, "reached_outlets": reached}


def checks(case: Case, estimate: Estimator | None = None) -> list[Check]:
    """The mesh checks of the mockup, in display order: problems first."""
    from ...selection import component_key, opening_key

    p = plan(case)
    out: list[Check] = []

    # cell count
    if p.n_cells > MANY_CELLS:
        out.append(Check(False, f"{fmt_cells(p.n_cells)} mailles : calcul très long",
                         "baissez la finesse", "warning", fix="Passer en Moyen",
                         fix_id="level:3"))

    # thin parts
    thin_ok = 0
    for comp in case.components:
        n, axis = cells_across(case, comp.name, p)
        need = MIN_CELLS_ACROSS if comp.power > 0 else MIN_CELLS_ACROSS_PASSIVE
        if n >= need:
            thin_ok += 1
            continue
        label = "maille" if n == 1 else "mailles"
        detail = ("trop peu pour la température" if n < 2 else
                  "juste suffisant pour la température")
        cost = ""
        if estimate is not None:
            ref = fix_thin(case, comp.name)
            fixed = with_changes(case, lambda d, r=ref, nm=comp.name: d.__setitem__(
                "refinements", [x for x in d.get("refinements") or [] if x["target"] != nm]
                + [r]))
            cost = fmt_cost(Cost(plan(fixed).n_cells - p.n_cells,
                                 estimate(fixed) - estimate(case)))
        out.append(Check(False, f"{comp.name} : {n} {label} d’épaisseur (axe {AXIS_NAMES[axis]})",
                         detail, "warning", component_key(comp.name),
                         fix=f"Raffiner {comp.name}", fix_id=f"thin:{comp.name}",
                         fix_cost=cost))

    # air path
    paths = air_paths(case, p)
    for name in paths["blocked"]:
        out.append(Check(False, f"{name} bouchée par une pièce",
                         "aucune maille d’air contre l’ouverture", "error", opening_key(name)))
    inlets = [o for o in case.openings if o.kind == OpeningKind.inlet]
    outlets = [o for o in case.openings if o.kind == OpeningKind.outlet]
    if inlets and outlets:
        if paths["reached_outlets"]:
            out.append(Check(True, "Volume d’air continu", "de l’entrée à la sortie"))
        else:
            out.append(Check(False, "Volume d’air coupé",
                             "l’air ne peut pas aller de l’entrée à la sortie", "error"))
    out.append(Check(bool(inlets and outlets), "Entrée et sortie détectées",
                     " · ".join(o.name for o in inlets + outlets) or "aucune ouverture",
                     "error"))

    # parts swallowed by an earlier part get no cell
    unmeshed = []
    for i, comp in enumerate(case.components):
        for prev in case.components[:i]:
            if all(prev.box.min[k] <= comp.box.min[k] + TOL
                   and comp.box.max[k] <= prev.box.max[k] + TOL for k in range(3)):
                unmeshed.append((comp.name, prev.name))
                break
    for name, inside in unmeshed:
        out.append(Check(False, f"{name} non maillée", f"entièrement dans {inside}", "error",
                         component_key(name)))
    total = len(case.components)
    if total:
        out.append(Check(not unmeshed, "Toutes les pièces maillées",
                         f"{total - len(unmeshed)} / {total}", "error"))
    if thin_ok == total and total:
        out.append(Check(True, "Épaisseurs suffisantes",
                         f"au moins {MIN_CELLS_ACROSS} mailles dans les sources de chaleur"))
    out.sort(key=lambda c: (c.ok, 0 if c.level == "error" else 1))
    return out


def apply_fix(data: dict, case: Case, fix_id: str) -> None:
    """Change ``data`` in place as the fix button ``fix_id`` says."""
    kind, _, arg = fix_id.partition(":")
    if kind == "level":
        data["precision"] = {"level": int(arg), "cell_size": None}
    elif kind == "thin":
        ref = fix_thin(case, arg)
        refs = [r for r in data.get("refinements") or [] if r.get("target") != arg]
        data["refinements"] = refs + [ref]
    else:
        raise ValueError(f"unknown fix {fix_id!r}")


# ---- the real grid, for the preview ------------------------------------------------
def node_coords(g: AxisGrid) -> list[float]:
    """Node positions of a graded axis, as blockMesh places them."""
    xs = [g.breakpoints[0]]
    for a, b, n, r in zip(g.breakpoints, g.breakpoints[1:], g.cells, g._ratios()):
        if n == 1 or abs(r - 1.0) < 1e-12:
            xs += [a + (b - a) * (k + 1) / n for k in range(n)]
            continue
        rc = r ** (1.0 / (n - 1))
        first = (b - a) * (rc - 1.0) / (rc**n - 1.0)
        xs += [a + first * (rc ** (k + 1) - 1.0) / (rc - 1.0) for k in range(n)]
        xs[-1] = b
    return xs


def cut_grid(case: Case, axis: int, position: float, p: MeshPlan | None = None):
    """The cells of the real mesh cut by the plane ``axis = position``, as a flat
    pyvista RectilinearGrid with cell data ``size_mm`` (local cell size) and
    ``solid`` (1 inside a part, 0 in the air)."""
    import numpy as np
    import pyvista as pv

    p = p or plan(case)
    coords = [np.asarray(node_coords(g)) for g in p.axes]
    E = case.enclosure.as_tuple()
    position = min(max(position, 0.0), E[axis])
    k = int(np.clip(np.searchsorted(coords[axis], position) - 1, 0, len(coords[axis]) - 2))
    thickness = coords[axis][k + 1] - coords[axis][k]
    xyz = [c for c in coords]
    xyz[axis] = np.array([position])
    grid = pv.RectilinearGrid(*xyz)
    # cell centres and sizes of the plane cells (cells of the 2 other axes)
    others = [i for i in range(3) if i != axis]
    ca = [(coords[i][:-1] + coords[i][1:]) / 2 for i in others]
    da = [np.diff(coords[i]) for i in others]
    # pyvista orders cells x fastest
    A, B = np.meshgrid(ca[0], ca[1], indexing="ij")
    dA, dB = np.meshgrid(da[0], da[1], indexing="ij")
    A, B, dA, dB = (v.ravel(order="F") for v in (A, B, dA, dB))
    size = np.cbrt(dA * dB * thickness) * 1000.0
    solid = np.zeros(len(A), dtype=np.int8)
    for comp in case.components:
        lo, hi = comp.box.min, comp.box.max
        if not lo[axis] - TOL <= position <= hi[axis] + TOL:
            continue
        inside = ((A > lo[others[0]]) & (A < hi[others[0]])
                  & (B > lo[others[1]]) & (B < hi[others[1]]))
        if comp.shape is not None:  # the cells the mesh gives the CAD shape
            from ....cad import voxelize

            pts: list = [None, None, None]
            pts[others[0]], pts[others[1]], pts[axis] = ca[0], ca[1], [position]
            in_shape = voxelize(comp.shape.triangles_in(comp.box), *pts)
            inside &= np.squeeze(in_shape, axis=axis).ravel(order="F")
        solid[inside] = 1
    grid.cell_data["size_mm"] = size
    grid.cell_data["solid"] = solid
    return grid


def default_cut(case: Case) -> tuple[int, float]:
    """A plane through the middle of the enclosure, across y (as in the mockup)."""
    return 1, case.enclosure.y / 2


def cut_through(case: Case, name: str, current_axis: int) -> tuple[int, float]:
    """A plane through the centre of part ``name`` that shows its thin direction:
    keep the current axis unless it is the thin one."""
    _, thin = cells_across(case, name)
    axis = current_axis if current_axis != thin else (thin + 1) % 3
    comp = next(c for c in case.components if c.name == name)
    return axis, (comp.box.min[axis] + comp.box.max[axis]) / 2


# ---- formatting -------------------------------------------------------------------
def fmt_cells(n: float) -> str:
    """8 k, 110 k, 1,2 M (French decimal comma)."""
    sign = "−" if n < 0 else ""
    n = abs(n)
    if n < 1000:
        return f"{sign}{round(n)}"
    if n < 999_500:
        k = n / 1000
        s = f"{k:.1f}" if k < 10 and round(k, 1) != round(k) else f"{round(k)}"
        return f"{sign}{s.replace('.', ',')} k"
    return f"{sign}{n / 1e6:.1f} M".replace(".", ",")


def fmt_duration(seconds: float) -> str:
    """45 s, 12 min, 1 h 20."""
    sign = "−" if seconds < 0 else ""
    s = abs(seconds)
    if s < 60:
        return f"{sign}{max(1, round(s))} s"
    if s < 3600:
        return f"{sign}{round(s / 60)} min"
    h, m = divmod(round(s / 60), 60)
    return f"{sign}{h} h {m:02d}" if m else f"{sign}{h} h"


def fmt_cost(cost: Cost) -> str:
    """'+6 k mailles, +2 min'."""
    def plus(s: str) -> str:
        return s if s.startswith("−") else "+" + s
    return f"{plus(fmt_cells(cost.cells))} mailles, {plus(fmt_duration(cost.seconds))}"


def data_case(data: dict) -> Case | None:
    try:
        return Case(**copy.deepcopy(data))
    except Exception:  # noqa: BLE001 - invalid while editing
        return None


__all__ = ["LEVEL_NAMES", "Check", "Cost", "LevelRow", "air_paths", "apply_fix", "at_level",
           "bulk_cell_size", "cell_size_range", "cells_across", "checks", "cut_grid",
           "cut_through", "default_cut", "default_refinement", "fix_thin", "fmt_cells",
           "fmt_cost", "fmt_duration", "level_rows", "node_coords", "plan",
           "refinement_cost", "with_changes"]
