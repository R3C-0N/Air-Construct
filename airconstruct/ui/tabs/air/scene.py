"""What the 3D view shows in the Air step.

Owned by: thread "Air". The model geometry (enclosure, parts), the openings drawn from
the raw data (so they stay visible while the case is invalid), a ring on the fans, a
mesh on the grilles, and six nearly invisible face planes that can be clicked: picking
one selects ``"face:<face>"`` and lets the user add an opening there.
"""

from __future__ import annotations

from ...theme import COLORS
from ...view import SceneItem, geometry_items
from .face_editor import opening_color
from .geometry import FACE_AXES, enclosure, opening_3d

FACE_PREFIX = "face:"
WALLS_KEY = "walls"


def face_key(face: str) -> str:
    return f"{FACE_PREFIX}{face}"


def air_items(data: dict, case) -> list[SceneItem]:
    import numpy as np
    import pyvista as pv

    items = [i for i in geometry_items(case) if not (i.key or "").startswith("opening:")] \
        if case is not None else []
    E = enclosure(data)
    if min(E) <= 0:
        return items
    diag = float(np.linalg.norm(E))
    if not items:
        items.append(SceneItem("enclosure", pv.Box((0, E[0], 0, E[1], 0, E[2])), "#555555",
                               style="wireframe", key="enclosure"))
    inset, eps = 0.002 * diag, 0.0005 * diag
    for face, (n, at_max, _ab) in FACE_AXES.items():
        lo, hi = [0.0] * 3, list(E)
        at = E[n] - inset if at_max else inset
        lo[n], hi[n] = at - eps, at + eps
        items.append(SceneItem(f"face_{face}", pv.Box((lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])),
                               COLORS["accent"], opacity=0.03, key=face_key(face),
                               highlight_opacity=0.18))
    t = 0.002 * diag
    for o in data.get("openings") or []:
        geo = opening_3d(data, o)
        if geo is None:
            continue
        lo, hi, n = geo
        lo, hi = list(lo), list(hi)
        lo[n] -= t / 2
        hi[n] += t / 2
        name = str(o.get("name"))
        key = f"opening:{name}"
        color = opening_color(o)
        items.append(SceneItem(name, pv.Box((lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])), color,
                               opacity=0.7, key=key))
        c = [(lo[i] + hi[i]) / 2 for i in range(3)]
        normal = [0.0, 0.0, 0.0]
        normal[n] = 1.0
        side = [hi[i] - lo[i] for i in range(3) if i != n]
        if o.get("fan"):
            r = 0.45 * min(side)
            ring = pv.Disc(center=c, inner=0.25 * r, outer=r, normal=normal, c_res=36)
            out = list(c)
            at_max = FACE_AXES[o["face"]][1]
            out[n] += (t if at_max else -t)
            ring.translate([out[i] - c[i] for i in range(3)], inplace=True)
            items.append(SceneItem(f"{name}_fan", ring, COLORS["text"], key=key,
                                   kwargs={"show_edges": False}))
        if o.get("grille"):
            items.append(SceneItem(f"{name}_grille", _grid(lo, hi, n), "#333333",
                                   style="wireframe", key=key))
    return items


def _grid(lo, hi, n: int, cells: int = 8):
    """A flat wire grid over an opening (the grille)."""
    import numpy as np
    import pyvista as pv

    a, b = [i for i in range(3) if i != n]
    ua = np.linspace(lo[a], hi[a], cells + 1)
    ub = np.linspace(lo[b], hi[b], cells + 1)
    ga, gb = np.meshgrid(ua, ub, indexing="ij")
    pts = np.zeros((*ga.shape, 3))
    pts[..., a] = ga
    pts[..., b] = gb
    pts[..., n] = hi[n]
    grid = pv.StructuredGrid(pts[..., 0], pts[..., 1], pts[..., 2])
    return grid
