"""Imported CAD parts in the project data: new project from a CAD file, parts added
to the current project, shape dropped back to a plain box.

Owned by: thread "Modèle" (see docs/ui-architecture.md). Plain functions on the
project data (no Qt), like :mod:`.geometry`. Reading the files is
:mod:`airconstruct.cad`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ....cad import Body, component
from . import geometry as geo

KIND = "cad"  # data["ui"]["model"]["kinds"] value of an imported part
MARGIN = 0.15  # room around the parts of a new project, fraction of their size
MIN_MARGIN = 0.005  # [m]


def _bounds(bodies: list[Body]) -> tuple[np.ndarray, np.ndarray]:
    los, his = zip(*(b.bounds for b in bodies))
    return np.min(los, axis=0), np.max(his, axis=0)


def add_bodies(data: dict, bodies: list[Body], source: str | None = None,
               material: str = "aluminium", grow: bool = True) -> list[str]:
    """Add one part per body, keeping their relative positions. The set is centred
    in the enclosure in x and y and put on its floor (an enclosure without a size is
    sized to hold it); with ``grow`` the enclosure is enlarged when the set does not
    fit.
    Several parts are grouped (group named after the file). Returns the new names."""
    if not bodies:
        return []
    lo, hi = _bounds(bodies)
    size = hi - lo
    E = np.array(geo.enclosure(data), dtype=float)
    room = size + 2 * np.maximum(MARGIN * size, MIN_MARGIN)
    if (E <= 0).any():
        E = room
    elif grow and (size > E + 1e-12).any():
        E = np.maximum(E, room)
    data["enclosure"] = {a: float(v) for a, v in zip("xyz", E)}
    target = np.array([(E[0] - size[0]) / 2, (E[1] - size[1]) / 2, 0.0])
    shift = target - lo
    names: list[str] = []
    m = geo.meta(data)
    for b in bodies:
        name = geo.unique_name(data, b.name or "piece", names)
        moved = Body(b.name, b.triangles + shift)
        geo.components(data).append(component(moved, name, source, material))
        m["kinds"][name] = KIND
        names.append(name)
    if len(names) > 1:
        gname = geo.unique_name({"components": [{"name": g} for g in m["groups"]]},
                                Path(source or "cao").stem or "cao")
        geo.group(data, names, gname)
    return names


def new_project(bodies: list[Body], name: str, source: str | None = None,
                material: str = "aluminium") -> dict:
    """A new project holding the bodies, the enclosure around them with some room
    (``MARGIN``), parts raised off the floor by the same margin. Openings are left to
    the Air tab."""
    lo, hi = _bounds(bodies)
    size = hi - lo
    margin = np.maximum(MARGIN * size, MIN_MARGIN)
    E = size + 2 * margin
    data = {"name": name, "enclosure": {a: float(v) for a, v in zip("xyz", E)},
            "ambient_temperature": 25.0, "components": [], "openings": []}
    names = add_bodies(data, bodies, source, material, grow=False)
    geo.move(data, names, [0.0, 0.0, float(margin[2])])
    return data


def shape_info(c: dict) -> str:
    """One line about a part's imported shape, "" if it has none."""
    s = c.get("shape")
    if not isinstance(s, dict) or not s.get("data"):
        return ""
    src = s.get("source")
    n = s.get("triangles")
    bits = [f"Forme importée de {src}" if src else "Forme importée"]
    if n:
        bits.append(f"{int(n):,} triangles".replace(",", " "))
    return " · ".join(bits)


def drop_shape(data: dict, part_names) -> list[str]:
    """Make the parts plain boxes again (their box is kept). Returns those changed."""
    out = []
    for n in part_names:
        c = geo.comp(data, n)
        if c is not None and c.get("shape") is not None:
            del c["shape"]
            if geo.read_meta(data)["kinds"].get(n) == KIND:
                geo.meta(data)["kinds"].pop(n, None)
            out.append(n)
    return out


def has_shape(data: dict, name: str) -> bool:
    c = geo.comp(data, name)
    return bool(c and isinstance(c.get("shape"), dict) and c["shape"].get("data"))
