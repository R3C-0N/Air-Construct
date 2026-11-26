"""Write a small, synthetic multi-region OpenFOAM case (ASCII) without OpenFOAM.

The layout matches what chtMultiRegionFoam writes after splitMeshRegions:
constant/<region>/polyMesh and <time>/<region>/{T,U,p,p_rgh}, one region "air" plus one
region per component, interfaces named "<a>_to_<b>". Fields come from user callables,
so tests know the exact expected values. The mesh is a uniform hex grid of the enclosure;
each cell belongs to the component whose box contains its centre, otherwise to "air".
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

from airconstruct.case import Case, Face

ScalarFn = Callable[[np.ndarray], np.ndarray]  # (n, 3) points -> (n,)
VectorFn = Callable[[np.ndarray], np.ndarray]  # (n, 3) points -> (n, 3)

_HEADER = """FoamFile
{{
    version     2.0;
    format      ascii;
    class       {cls};
    location    "{loc}";
    object      {obj};
}}
"""


def _write(path: Path, cls: str, obj: str, body: str, loc: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER.format(cls=cls, loc=loc, obj=obj) + body)


def _list(items, fmt) -> str:
    return f"{len(items)}\n(\n" + "\n".join(fmt(v) for v in items) + "\n)\n"


def _scalars(v: np.ndarray) -> str:
    return "nonuniform List<scalar> " + _list(v, lambda a: f"{a:.10g}") + ";"


def _vectors(v: np.ndarray) -> str:
    return "nonuniform List<vector> " + _list(v, lambda a: f"({a[0]:.10g} {a[1]:.10g} {a[2]:.10g})") + ";"


_FACE_AXIS = {Face.xmin: (0, 0), Face.xmax: (0, 1), Face.ymin: (1, 0), Face.ymax: (1, 1),
              Face.zmin: (2, 0), Face.zmax: (2, 1)}


def _field(tdir: Path, name: str, cls: str, fn, fmt, cell_pts, patch_pts) -> None:
    bf = [f"    {n}\n    {{\n        type calculated;\n        value {fmt(fn(fc))}\n    }}"
          for n, fc in patch_pts.items()]
    dims = {"T": "[0 0 0 1 0 0 0]", "U": "[0 1 -1 0 0 0 0]"}.get(name, "[1 -1 -2 0 0 0 0]")
    _write(tdir / name, cls, name,
           f"dimensions {dims};\ninternalField {fmt(fn(cell_pts))}\n"
           "boundaryField\n{\n" + "\n".join(bf) + "\n}\n",
           f"{tdir.parent.name}/{tdir.name}")


def write_case(
    case: Case,
    case_dir: Path,
    divisions: tuple[int, int, int],
    T: Callable[[np.ndarray, str], np.ndarray],
    U: VectorFn,
    p: ScalarFn,
    times: tuple[str, ...] = ("0", "100"),
    rho: float = 1.2,
) -> dict[str, int]:
    """Write the case; T(points, region) in Kelvin, U and p only for "air".
    p_rgh = p - rho * g . h with g = (0, 0, -9.81). Returns {region: n_cells}."""
    nx, ny, nz = divisions
    L = np.array(case.enclosure.as_tuple())
    h = L / np.array(divisions)
    xs = [np.linspace(0, L[a], n + 1) for a, n in enumerate(divisions)]

    def pid(i, j, k):
        return i + (nx + 1) * (j + (ny + 1) * k)

    gi, gj, gk = np.meshgrid(*xs, indexing="ij")
    all_points = np.stack([gi.ravel("F"), gj.ravel("F"), gk.ravel("F")], axis=1)

    # region of each cell
    ci, cj, ck = np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij")
    ijk = np.stack([ci.ravel("F"), cj.ravel("F"), ck.ravel("F")], axis=1)
    centres = (ijk + 0.5) * h
    region = np.array(["air"] * len(ijk), dtype=object)
    for comp in case.components:
        lo, hi = np.array(comp.box.min), np.array(comp.box.max)
        inside = np.all((centres > lo) & (centres < hi), axis=1)
        region[inside] = comp.name

    def cell(i, j, k):
        if 0 <= i < nx and 0 <= j < ny and 0 <= k < nz:
            return i + nx * (j + ny * k)
        return -1

    def face_quad(axis, i, j, k):
        """Quad on the +axis side of cell (i,j,k), normal along +axis."""
        if axis == 0:
            return [pid(i + 1, j, k), pid(i + 1, j + 1, k), pid(i + 1, j + 1, k + 1), pid(i + 1, j, k + 1)]
        if axis == 1:
            return [pid(i, j + 1, k), pid(i, j + 1, k + 1), pid(i + 1, j + 1, k + 1), pid(i + 1, j + 1, k)]
        return [pid(i, j, k + 1), pid(i + 1, j, k + 1), pid(i + 1, j + 1, k + 1), pid(i, j + 1, k + 1)]

    def opening_patch(axis, side, centre):
        for o in case.openings:
            a, s = _FACE_AXIS[o.face]
            if (a, s) != (axis, side):
                continue
            inplane = [c for c in range(3) if c != axis]
            if all(abs(centre[c] - o.center[n]) < o.size[n] / 2 for n, c in enumerate(inplane)):
                return o.name
        return "walls"

    # every face of the grid: (quad oriented +axis, minus cell, plus cell, centre)
    faces = []
    for axis in range(3):
        for k in range(-1 if axis == 2 else 0, nz):
            for j in range(-1 if axis == 1 else 0, ny):
                for i in range(-1 if axis == 0 else 0, nx):
                    nb = [i, j, k]
                    nb[axis] += 1
                    a, b = cell(i, j, k), cell(*nb)
                    if a < 0 and b < 0:
                        continue
                    quad = face_quad(axis, i, j, k)
                    faces.append((quad, a, b, all_points[quad].mean(axis=0), axis))

    regions = ["air"] + [c.name for c in case.components]
    counts: dict[str, int] = {}
    for reg in regions:
        cells = np.flatnonzero(region == reg)
        if len(cells) == 0:
            continue
        local = {c: n for n, c in enumerate(cells)}
        internal, boundary = [], {}
        for quad, a, b, ctr, axis in faces:
            ina, inb = a >= 0 and region[a] == reg, b >= 0 and region[b] == reg
            if ina and inb:
                internal.append((local[a], local[b], quad, ctr))
            elif ina or inb:
                own = local[a] if ina else local[b]
                other = b if ina else a
                q = quad if ina else quad[::-1]
                if other < 0:
                    name = opening_patch(axis, 1 if ina else 0, ctr) if reg == "air" else "walls"
                else:
                    name = f"{reg}_to_{region[other]}"
                boundary.setdefault(name, []).append((own, q, ctr))
        internal.sort(key=lambda f: (f[0], f[1]))
        patch_names = sorted(boundary, key=lambda n: (n != "walls", n))
        flist = [f[2] for f in internal] + [f[1] for n in patch_names for f in boundary[n]]
        owner = [f[0] for f in internal] + [f[0] for n in patch_names for f in boundary[n]]
        neigh = [f[1] for f in internal]
        used = sorted({v for q in flist for v in q})
        remap = {v: n for n, v in enumerate(used)}
        mesh = case_dir / "constant" / reg / "polyMesh"
        loc = f"constant/{reg}/polyMesh"
        _write(mesh / "points", "vectorField", "points",
               _list(all_points[used], lambda a: f"({a[0]:.10g} {a[1]:.10g} {a[2]:.10g})"), loc)
        _write(mesh / "faces", "faceList", "faces",
               _list([[remap[v] for v in q] for q in flist],
                     lambda q: "4(" + " ".join(map(str, q)) + ")"), loc)
        _write(mesh / "owner", "labelList", "owner", _list(owner, str), loc)
        _write(mesh / "neighbour", "labelList", "neighbour", _list(neigh, str), loc)
        start, body = len(internal), []
        for n in patch_names:
            kind = "patch" if n in {o.name for o in case.openings} else "wall"
            body.append(f"    {n}\n    {{\n        type {kind};\n        nFaces {len(boundary[n])};\n"
                        f"        startFace {start};\n    }}")
            start += len(boundary[n])
        _write(mesh / "boundary", "polyBoundaryMesh", "boundary",
               f"{len(patch_names)}\n(\n" + "\n".join(body) + "\n)\n", loc)

        cpts = centres[cells]
        bfaces = {n: np.array([f[2] for f in boundary[n]]) for n in patch_names}
        for t in times:
            tdir = case_dir / t / reg
            _field(tdir, "T", "volScalarField", lambda x, reg=reg: T(x, reg), _scalars,
                   cpts, bfaces)
            if reg == "air":
                _field(tdir, "U", "volVectorField", U, _vectors, cpts, bfaces)
                _field(tdir, "p", "volScalarField", p, _scalars, cpts, bfaces)
                _field(tdir, "p_rgh", "volScalarField",
                       lambda x: p(x) - rho * 9.81 * x[:, 2], _scalars, cpts, bfaces)
        counts[reg] = len(cells)

    solids = " ".join(c.name for c in case.components if c.name in counts)
    _write(case_dir / "constant" / "regionProperties", "dictionary", "regionProperties",
           f"regions\n(\n    fluid (air)\n    solid ({solids})\n);\n", "constant")
    _write(case_dir / "system" / "controlDict", "dictionary", "controlDict",
           "application chtMultiRegionFoam;\nstartFrom latestTime;\nendTime 100;\n"
           "deltaT 1;\nwriteControl timeStep;\nwriteInterval 100;\n", "system")
    (case_dir / "case.foam").touch()
    return counts
