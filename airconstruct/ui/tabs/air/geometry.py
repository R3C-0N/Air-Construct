"""Plain geometry of openings on the enclosure faces (no Qt), used by the Air tab.

Owned by: thread "Air". Works on the raw project data (dicts shaped like
``Case.model_dump(mode="json")``) because the data may be invalid while the user edits.

An opening lies on one enclosure face; its ``center`` and ``size`` are the two in-plane
coordinates in axis order (xmin/xmax: (y, z); ymin/ymax: (x, z); zmin/zmax: (x, y)),
so in the 2D face editor the first one is horizontal and the second one vertical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FACES = ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
AXES = "xyz"
# face -> (normal axis, at the max side, (horizontal axis, vertical axis))
FACE_AXES: dict[str, tuple[int, bool, tuple[int, int]]] = {
    "xmin": (0, False, (1, 2)),
    "xmax": (0, True, (1, 2)),
    "ymin": (1, False, (0, 2)),
    "ymax": (1, True, (0, 2)),
    "zmin": (2, False, (0, 1)),
    "zmax": (2, True, (0, 1)),
}
FACE_LABELS = {
    "xmin": "Gauche (x min)", "xmax": "Droite (x max)",
    "ymin": "Avant (y min)", "ymax": "Arrière (y max)",
    "zmin": "Dessous (z min)", "zmax": "Dessus (z max)",
}
TOL = 1e-9
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def enclosure(data: dict) -> tuple[float, float, float]:
    e = data.get("enclosure") or {}
    return (_num(e.get("x")), _num(e.get("y")), _num(e.get("z")))


def face_size(data: dict, face: str) -> tuple[float, float]:
    """(width, height) of a face in metres, in its in-plane axis order."""
    E = enclosure(data)
    _, _, (a, b) = FACE_AXES[face]
    return (E[a], E[b])


def face_axis_names(face: str) -> tuple[str, str]:
    _, _, (a, b) = FACE_AXES[face]
    return (AXES[a], AXES[b])


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle in a face's plane: min corner u0, v0 and max corner u1, v1."""

    u0: float
    v0: float
    u1: float
    v1: float

    @classmethod
    def from_center(cls, center, size) -> Rect:
        cu, cv = _num(center[0]), _num(center[1])
        su, sv = _num(size[0]), _num(size[1])
        return cls(cu - su / 2, cv - sv / 2, cu + su / 2, cv + sv / 2)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.u0 + self.u1) / 2, (self.v0 + self.v1) / 2)

    @property
    def size(self) -> tuple[float, float]:
        return (self.u1 - self.u0, self.v1 - self.v0)

    def overlap_area(self, other: Rect) -> float:
        du = min(self.u1, other.u1) - max(self.u0, other.u0)
        dv = min(self.v1, other.v1) - max(self.v0, other.v0)
        return du * dv if du > TOL and dv > TOL else 0.0

    def inside(self, w: float, h: float, tol: float = 1e-7) -> bool:
        return self.u0 >= -tol and self.v0 >= -tol and self.u1 <= w + tol and self.v1 <= h + tol


def opening_rect(o: dict) -> Rect | None:
    try:
        return Rect.from_center(o["center"], o["size"])
    except (KeyError, TypeError, IndexError):
        return None


def edge_distances(rect: Rect, w: float, h: float) -> dict[str, float]:
    """Distances from the rectangle to the four edges of a w x h face."""
    return {"left": rect.u0, "right": w - rect.u1, "bottom": rect.v0, "top": h - rect.v1}


def clamp_center(center: tuple[float, float], size: tuple[float, float], w: float,
                 h: float) -> tuple[float, float]:
    """Keep a rectangle of ``size`` inside the face (centred when it is too large)."""
    out = []
    for c, s, d in ((center[0], size[0], w), (center[1], size[1], h)):
        out.append(d / 2 if s >= d else min(max(c, s / 2), d - s / 2))
    return (out[0], out[1])


def snap_center(center: tuple[float, float], size: tuple[float, float], w: float, h: float,
                tol: float, others: list[Rect] = ()) -> tuple[tuple[float, float],
                                                           tuple[str | None, str | None]]:
    """Snap a moved rectangle to the face edges, the face centre lines and the edges of
    the other openings when closer than ``tol`` (metres). Returns the new centre and
    what each axis snapped to (``"edge"``, ``"center"``, ``"opening"`` or None)."""
    result = []
    snapped: list[str | None] = []
    for k, (c, s, d) in enumerate(((center[0], size[0], w), (center[1], size[1], h))):
        cands = [(d / 2, "center"), (s / 2, "edge"), (d - s / 2, "edge")]
        for r in others:
            lo, hi = (r.u0, r.u1) if k == 0 else (r.v0, r.v1)
            cands += [(lo - s / 2, "opening"), (hi + s / 2, "opening"),
                      (lo + s / 2, "opening"), (hi - s / 2, "opening"),
                      ((lo + hi) / 2, "opening")]
        best = min(cands, key=lambda t: abs(t[0] - c))
        if abs(best[0] - c) <= tol:
            result.append(best[0])
            snapped.append(best[1])
        else:
            result.append(c)
            snapped.append(None)
    return (result[0], result[1]), (snapped[0], snapped[1])


def snap_value(v: float, targets: list[float], tol: float) -> float:
    best = min(targets, key=lambda t: abs(t - v), default=None)
    return best if best is not None and abs(best - v) <= tol else v


def openings_on(data: dict, face: str) -> list[dict]:
    return [o for o in data.get("openings") or [] if o.get("face") == face]


def used_names(data: dict) -> set[str]:
    return ({str(c.get("name")) for c in data.get("components") or []}
            | {str(o.get("name")) for o in data.get("openings") or []})


def unique_name(data: dict, base: str) -> str:
    used = used_names(data)
    if base not in used:
        return base
    i = 2
    while f"{base}_{i}" in used:
        i += 1
    return f"{base}_{i}"


def free_spot(data: dict, face: str, size: tuple[float, float],
              prefer: tuple[float, float] | None = None) -> tuple[float, float]:
    """A centre for a new rectangle of ``size`` on ``face``: ``prefer`` (default the face
    centre) if free, else the free grid point closest to it, else ``prefer`` anyway."""
    w, h = face_size(data, face)
    want = clamp_center(prefer or (w / 2, h / 2), size, w, h)
    others = [r for r in (opening_rect(o) for o in openings_on(data, face)) if r]

    def free(c) -> bool:
        r = Rect.from_center(c, size)
        return all(r.overlap_area(o) <= 0 for o in others)

    if free(want):
        return want
    n = 12
    cands = []
    for i in range(n + 1):
        for j in range(n + 1):
            c = clamp_center((w * i / n, h * j / n), size, w, h)
            if free(c):
                cands.append(c)
    if not cands:
        return want
    return min(cands, key=lambda c: (c[0] - want[0]) ** 2 + (c[1] - want[1]) ** 2)


def default_size(data: dict, face: str, fan: bool = False) -> tuple[float, float]:
    """A sensible first size: 40 % of the shorter side (square), at most 80 mm for fans."""
    w, h = face_size(data, face)
    s = 0.4 * min(w, h)
    if fan:
        s = min(s, 0.08)
    s = round(s * 1000) / 1000 or s  # whole millimetres
    return (s, s)


def point_to_face(point: tuple[float, float, float], face: str) -> tuple[float, float]:
    """In-plane coordinates of a 3D point on a face."""
    _, _, (a, b) = FACE_AXES[face]
    return (point[a], point[b])


def nearest_face(data: dict, point: tuple[float, float, float]) -> str:
    """The enclosure face closest to a 3D point (a picked point on the box)."""
    E = enclosure(data)
    best, dist = "xmin", float("inf")
    for face, (n, at_max, _) in FACE_AXES.items():
        d = abs(point[n] - (E[n] if at_max else 0.0))
        if d < dist:
            best, dist = face, d
    return best


def face_quad(data: dict, face: str, inset: float = 0.0) -> tuple[list[float], list[float]]:
    """(lo, hi) 3D corners of a face's plane (zero thickness along the normal)."""
    E = enclosure(data)
    n, at_max, (a, b) = FACE_AXES[face]
    lo, hi = [0.0] * 3, [0.0] * 3
    lo[a], hi[a] = inset, E[a] - inset
    lo[b], hi[b] = inset, E[b] - inset
    lo[n] = hi[n] = E[n] if at_max else 0.0
    return lo, hi


def opening_3d(data: dict, o: dict) -> tuple[list[float], list[float], int] | None:
    """(lo, hi, normal axis) of an opening's rectangle in 3D, or None if malformed."""
    face = o.get("face")
    r = opening_rect(o)
    if face not in FACE_AXES or r is None:
        return None
    E = enclosure(data)
    n, at_max, (a, b) = FACE_AXES[face]
    lo, hi = [0.0] * 3, [0.0] * 3
    lo[a], hi[a], lo[b], hi[b] = r.u0, r.u1, r.v0, r.v1
    lo[n] = hi[n] = E[n] if at_max else 0.0
    return lo, hi, n


def component_footprints(data: dict, face: str) -> list[tuple[str, Rect]]:
    """Components lying against a face (touching the wall), as rectangles on it."""
    E = enclosure(data)
    n, at_max, (a, b) = FACE_AXES[face]
    out = []
    for c in data.get("components") or []:
        try:
            o = c["box"]["origin"]
            s = c["box"]["size"]
            lo = [_num(o[k]) for k in AXES]
            sz = [_num(s[k]) for k in AXES]
        except (KeyError, TypeError):
            continue
        hi = [lo[i] + sz[i] for i in range(3)]
        on = abs(hi[n] - E[n]) < 1e-7 if at_max else abs(lo[n]) < 1e-7
        if on:
            out.append((str(c.get("name")), Rect(lo[a], lo[b], hi[a], hi[b])))
    return out
