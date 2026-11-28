"""Where a probe goes when it is placed or dragged with the mouse in the 3D view.

Owned by: thread "Simulation" (see docs/ui-architecture.md).

Plain functions on the project data (SI units), tested without Qt or a 3D view:

- a click on a part puts the probe in the air just off the clicked face, where a
  thermocouple taped to it would read (Alt: just inside the part);
- a click elsewhere in the box puts it in the air, on the plane through the middle
  of the enclosure that faces the camera;
- a dragged probe stays inside the enclosure.
"""

from __future__ import annotations

import math

from ...view import drag_normal, ray_plane

PROBE_GAP = 0.002  # distance from the clicked face [m], same as "+ Sonde"
INSET = 1e-4  # keep probes strictly inside the enclosure [m]
STEP = 0.0005  # dragged probes move by half millimetres unless Alt is held


def enclosure(data: dict) -> tuple[float, float, float]:
    e = data.get("enclosure") or {}
    return tuple(float(e.get(a, 0.0) or 0.0) for a in "xyz")


def part_box(data: dict, name: str) -> tuple[list[float], list[float]] | None:
    for c in data.get("components") or []:
        if c.get("name") == name:
            try:
                o, s = c["box"]["origin"], c["box"]["size"]
                lo = [float(o[a]) for a in "xyz"]
                return lo, [lo[i] + float(s[a]) for i, a in enumerate("xyz")]
            except (KeyError, TypeError, ValueError):
                return None
    return None


def clamp_inside(data: dict, point) -> list[float]:
    E = enclosure(data)
    return [min(max(float(point[i]), INSET), max(E[i] - INSET, INSET)) for i in range(3)]


def _unit(v) -> tuple[float, float, float] | None:
    n = math.sqrt(sum(float(c) ** 2 for c in v))
    return tuple(float(c) / n for c in v) if n > 1e-9 else None


def _face_normal(lo, hi, point) -> tuple[float, float, float]:
    """Outward normal of the box face nearest ``point``."""
    best, dist = (0.0, 0.0, 1.0), math.inf
    for i in range(3):
        for sign, at in ((-1.0, lo[i]), (1.0, hi[i])):
            d = abs(point[i] - at)
            if d < dist:
                best, dist = tuple(sign if j == i else 0.0 for j in range(3)), d
    return best


def on_part(data: dict, name: str, point, normal=None, direction=(0.0, 0.0, -1.0),
            inside: bool = False) -> dict | None:
    """Probe point for a click on part ``name`` at ``point`` (on its surface).

    ``normal`` is the surface normal the picker gave (unknown: taken from the
    nearest face of the part's box). Outside the face by :data:`PROBE_GAP`, or just
    inside the part when ``inside`` (at most half its thickness)."""
    box = part_box(data, name)
    if box is None:
        return None
    lo, hi = box
    n = _unit(normal) if normal is not None else None
    if n is None or max(abs(c) for c in n) < 0.9:  # an edge or a corner: use the face
        n = _face_normal(lo, hi, point)
    else:  # the box is axis aligned: snap the normal to its axis
        i = max(range(3), key=lambda k: abs(n[k]))
        n = tuple((1.0 if n[i] > 0 else -1.0) if j == i else 0.0 for j in range(3))
    if sum(a * b for a, b in zip(n, direction)) > 0:  # facing away from the viewer
        n = tuple(-c for c in n)
    if inside:
        axis = max(range(3), key=lambda k: abs(n[k]))
        depth = min(PROBE_GAP, (hi[axis] - lo[axis]) / 2)
        p = [point[i] - n[i] * depth for i in range(3)]
        p = [min(max(p[i], lo[i]), hi[i]) for i in range(3)]
    else:
        p = [point[i] + n[i] * PROBE_GAP for i in range(3)]
    return dict(zip("xyz", clamp_inside(data, p)))


def in_air(data: dict, origin, direction) -> dict | None:
    """Probe point for a click that hit no part: where the ray through the cursor
    meets the middle plane of the enclosure facing the camera; None when that is
    outside the box."""
    E = enclosure(data)
    if min(E) <= 0:
        return None
    centre = tuple(v / 2 for v in E)
    hit = ray_plane(origin, direction, centre, drag_normal(direction))
    if hit is None or any(hit[i] < -1e-9 or hit[i] > E[i] + 1e-9 for i in range(3)):
        return None
    return dict(zip("xyz", clamp_inside(data, hit)))


def dragged(data: dict, start, delta, free: bool = False) -> list[float]:
    """Where a probe at ``start`` dragged by ``delta`` goes: inside the enclosure,
    on half millimetres unless ``free``."""
    p = [float(start[i]) + float(delta[i]) for i in range(3)]
    if not free:
        p = [round(round(v / STEP) * STEP, 9) if abs(float(delta[i])) > 1e-12 else v
             for i, v in enumerate(p)]
    return clamp_inside(data, p)
