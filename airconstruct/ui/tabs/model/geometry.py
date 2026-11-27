"""Editing operations of the Modèle tab, as plain functions on the project data.

Owned by: thread "Modèle" (see docs/ui-architecture.md).

Every function works on the data dict (``Project.data``, SI units) in place, so a
tab action is ``project.edit(label, lambda d: geometry.something(d, ...))`` and is
one undo step. Nothing here needs Qt, so it is tested directly.

UI-only metadata of the parts lives under ``data["ui"]["model"]``:

- ``groups``: ``{group name: [part names]}`` (parts moved together)
- ``hidden``: parts not drawn in the 3D view
- ``locked``: parts that handles and moves leave alone
- ``kinds``: ``{part name: library kind}`` ("chip", "fan"...), for labels
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable

from ....case import Face

AXES = "xyz"
EPS = 1e-9
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
RESERVED = {"air", "walls", "defaultFaces"} | {f"walls_{f.value}" for f in Face}


# --------------------------------------------------------------------- metadata
def meta(data: dict) -> dict:
    """``data["ui"]["model"]``, created on first use."""
    ui = data.setdefault("ui", {})
    if not isinstance(ui, dict):
        ui = data["ui"] = {}
    m = ui.setdefault("model", {})
    m.setdefault("groups", {})
    m.setdefault("hidden", [])
    m.setdefault("locked", [])
    m.setdefault("kinds", {})
    return m


def read_meta(data: dict) -> dict:
    """Metadata for reading only (does not add keys to ``data``)."""
    m = ((data.get("ui") or {}).get("model") or {}) if isinstance(data.get("ui"), dict) else {}
    return {"groups": dict(m.get("groups") or {}), "hidden": list(m.get("hidden") or []),
            "locked": list(m.get("locked") or []), "kinds": dict(m.get("kinds") or {})}


# --------------------------------------------------------------------- parts
def components(data: dict) -> list[dict]:
    return data.setdefault("components", []) if isinstance(data, dict) else []


def names(data: dict) -> list[str]:
    return [str(c.get("name")) for c in data.get("components") or []]


def comp(data: dict, name: str) -> dict | None:
    return next((c for c in data.get("components") or [] if c.get("name") == name), None)


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def lo_hi(c: dict) -> tuple[list[float], list[float]]:
    box = c.get("box") or {}
    o, s = box.get("origin") or {}, box.get("size") or {}
    lo = [_f(o.get(a)) for a in AXES]
    return lo, [lo[i] + _f(s.get(a)) for i, a in enumerate(AXES)]


def set_lo_size(c: dict, lo: Iterable[float], size: Iterable[float]) -> None:
    c["box"] = {"origin": {a: float(v) for a, v in zip(AXES, lo)},
                "size": {a: float(v) for a, v in zip(AXES, size)}}


def enclosure(data: dict) -> list[float]:
    e = data.get("enclosure") or {}
    return [_f(e.get(a)) for a in AXES]


def bbox(data: dict, part_names: Iterable[str]) -> tuple[list[float], list[float]] | None:
    """Bounding box of several parts, None when none exists."""
    los, his = [], []
    for n in part_names:
        c = comp(data, n)
        if c is not None:
            lo, hi = lo_hi(c)
            los.append(lo)
            his.append(hi)
    if not los:
        return None
    return [min(v[i] for v in los) for i in range(3)], [max(v[i] for v in his) for i in range(3)]


def taken_names(data: dict) -> set[str]:
    out = set(names(data)) | {str(o.get("name")) for o in data.get("openings") or []}
    out |= {str(p.get("name")) for p in data.get("probes") or []}
    return out | RESERVED | {str(data.get("fluid") or "air")}


def unique_name(data: dict, base: str, extra: Iterable[str] = ()) -> str:
    """``base`` if free, else ``base_2``, ``base_3``... (a trailing ``_N`` is dropped
    from ``base`` first, so copies of ``ram_2`` are ``ram_3``, not ``ram_2_2``)."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", base or "piece")
    if not base[0].isalpha():
        base = "p_" + base
    taken = taken_names(data) | set(extra)
    if base not in taken:
        return base
    stem = re.sub(r"_\d+$", "", base) or base
    i = 2
    while f"{stem}_{i}" in taken:
        i += 1
    return f"{stem}_{i}"


# --------------------------------------------------------------------- groups
def group_of(data: dict, name: str) -> str | None:
    for g, members in read_meta(data)["groups"].items():
        if name in members:
            return g
    return None


def with_groups(data: dict, part_names: Iterable[str]) -> list[str]:
    """The parts plus every other member of their groups (order kept, no duplicates)."""
    groups = read_meta(data)["groups"]
    out: list[str] = []
    for n in part_names:
        g = next((g for g, m in groups.items() if n in m), None)
        for m in ([n] if g is None else [n, *groups[g]]):
            if m not in out and comp(data, m) is not None:
                out.append(m)
    return out


def group(data: dict, part_names: list[str], gname: str | None = None) -> str:
    """Put the parts in one new group (taken out of their previous groups)."""
    m = meta(data)
    for members in m["groups"].values():
        members[:] = [n for n in members if n not in part_names]
    gname = gname or unique_name({"components": [{"name": g} for g in m["groups"]]}, "groupe")
    m["groups"][gname] = list(part_names)
    _drop_empty_groups(m)
    return gname


def ungroup(data: dict, part_names: Iterable[str]) -> None:
    """Dissolve every group that holds one of the parts."""
    m = meta(data)
    for g in [g for g, members in m["groups"].items() if set(members) & set(part_names)]:
        del m["groups"][g]


def _drop_empty_groups(m: dict) -> None:
    for g in [g for g, members in m["groups"].items() if len(members) < 2]:
        del m["groups"][g]


def set_flag(data: dict, flag: str, part_names: Iterable[str], on: bool) -> None:
    """Add (on) or remove the parts from ``hidden`` or ``locked``."""
    m = meta(data)
    cur = [n for n in m[flag] if n not in set(part_names)]
    m[flag] = cur + (list(part_names) if on else [])


# --------------------------------------------------------------------- move, resize
def snap(v: float, step: float | None) -> float:
    if not step:
        return v
    return round(round(v / step) * step, 12)


def move(data: dict, part_names: Iterable[str], delta: Iterable[float],
         groups: bool = True) -> list[str]:
    """Translate the parts (and their groups) by ``delta``; locked parts stay."""
    d = list(delta)
    moved = with_groups(data, part_names) if groups else list(part_names)
    locked = set(read_meta(data)["locked"])
    out = []
    for n in moved:
        c = comp(data, n)
        if c is None or n in locked:
            continue
        lo, hi = lo_hi(c)
        set_lo_size(c, [lo[i] + d[i] for i in range(3)], [hi[i] - lo[i] for i in range(3)])
        out.append(n)
    return out


def set_position(data: dict, part_names: list[str], axis: int, value: float) -> None:
    """Put the min corner of the parts' bounding box at ``value`` along ``axis``
    (the parts and their groups move together)."""
    bb = bbox(data, with_groups(data, part_names))
    if bb is None:
        return
    delta = [0.0, 0.0, 0.0]
    delta[axis] = value - bb[0][axis]
    move(data, part_names, delta)


def set_size(data: dict, part_names: Iterable[str], axis: int, value: float) -> None:
    """Set the size of each part along ``axis`` (its min corner stays)."""
    locked = set(read_meta(data)["locked"])
    for n in part_names:
        c = comp(data, n)
        if c is None or n in locked:
            continue
        lo, hi = lo_hi(c)
        size = [hi[i] - lo[i] for i in range(3)]
        size[axis] = value
        set_lo_size(c, lo, size)


def move_by(data: dict, part_names: list[str], raw: Iterable[float],
            step: float | None) -> list[str]:
    """Move by ``raw`` rounded to whole grid steps along each axis (so a part resting
    on another stays on it when dragged sideways); an axis not dragged stays."""
    delta = [0.0 if abs(v) < 1e-7 else snap(v, step) for v in raw]
    if all(abs(v) < EPS for v in delta):
        return []
    return move(data, part_names, delta)


def apply_bounds(data: dict, name: str, bounds: Iterable[float], mode: str,
                 step: float | None) -> None:
    """Result of a 3D handle on part ``name``: ``bounds`` is (xmin, xmax, ymin, ymax,
    zmin, zmax). ``mode`` "move" keeps the size and moves the part (and its group)
    by a whole number of grid steps; "resize" puts each dragged face on the grid
    (a face that did not move stays where it was)."""
    c = comp(data, name)
    if c is None or name in read_meta(data)["locked"]:
        return
    b = list(bounds)
    lo, hi = lo_hi(c)
    new_lo = [b[0], b[2], b[4]]
    new_hi = [b[1], b[3], b[5]]
    if mode == "move":
        move_by(data, [name], [new_lo[i] - lo[i] for i in range(3)], step)
        return
    tol = 1e-7
    min_size = step or 1e-4
    for i in range(3):
        lo_moved, hi_moved = abs(new_lo[i] - lo[i]) >= tol, abs(new_hi[i] - hi[i]) >= tol
        if not (lo_moved or hi_moved):
            continue
        a = snap(new_lo[i], step) if lo_moved else lo[i]
        z = snap(new_hi[i], step) if hi_moved else hi[i]
        if z - a < min_size:  # never flip or flatten a part
            if lo_moved:
                a = z - min_size
            else:
                z = a + min_size
        lo[i], hi[i] = a, z
    set_lo_size(c, lo, [hi[i] - lo[i] for i in range(3)])


# --------------------------------------------------------------------- rest on
def _xy_overlap(a_lo, a_hi, b_lo, b_hi) -> bool:
    return all(a_lo[i] < b_hi[i] - EPS and b_lo[i] < a_hi[i] - EPS for i in (0, 1))


def support_of(data: dict, name: str) -> str | None:
    """What the part rests on: ``"floor"``, the name of a part whose top face its
    bottom face touches (with some overlap in plan), or None (floating)."""
    c = comp(data, name)
    if c is None:
        return None
    lo, hi = lo_hi(c)
    if abs(lo[2]) < 1e-7:
        return "floor"
    for o in data.get("components") or []:
        if o is c:
            continue
        olo, ohi = lo_hi(o)
        if abs(ohi[2] - lo[2]) < 1e-7 and _xy_overlap(lo, hi, olo, ohi):
            return str(o.get("name"))
    return None


def drop_height(data: dict, name: str, ignore: Iterable[str] = ()) -> float:
    """Height the part falls to: the highest top, not above its own bottom, of the
    parts under its footprint; 0 (the floor) when nothing is under it."""
    c = comp(data, name)
    lo, hi = lo_hi(c)
    skip = set(ignore) | {name}
    best = 0.0
    for o in data.get("components") or []:
        if o.get("name") in skip:
            continue
        olo, ohi = lo_hi(o)
        if _xy_overlap(lo, hi, olo, ohi) and ohi[2] <= lo[2] + 1e-7:
            best = max(best, ohi[2])
    return best


def rest_on(data: dict, part_names: list[str], target: str | None = None) -> None:
    """"Poser sur": put each part (with its group) on ``target``'s top face, on the
    floor (``target="floor"``), or, with no target, let it fall onto whatever is
    under it. A part that does not overlap the target in plan is centred on it."""
    done: set[str] = set()
    for n in part_names:
        if n in done or comp(data, n) is None:
            continue
        members = with_groups(data, [n])
        done |= set(members)
        lo, hi = lo_hi(comp(data, n))
        delta = [0.0, 0.0, 0.0]
        if target is None:
            delta[2] = drop_height(data, n, members) - lo[2]
        elif target == "floor":
            delta[2] = -lo[2]
        else:
            t = comp(data, target)
            if t is None or target in members:
                continue
            tlo, thi = lo_hi(t)
            delta[2] = thi[2] - lo[2]
            if not _xy_overlap(lo, hi, tlo, thi):
                for i in (0, 1):
                    delta[i] = (tlo[i] + thi[i]) / 2 - (lo[i] + hi[i]) / 2
        move(data, [n], delta)


# --------------------------------------------------------------------- align
ALIGN = {
    "xmin": "Aligner à gauche (x min)", "xcenter": "Centrer en x", "xmax": "Aligner à droite (x max)",
    "ymin": "Aligner devant (y min)", "ycenter": "Centrer en y", "ymax": "Aligner derrière (y max)",
    "zmin": "Aligner en bas (z min)", "zmax": "Aligner en haut (z max)",
    "xdist": "Répartir en x", "ydist": "Répartir en y",
}


def align(data: dict, part_names: list[str], how: str) -> None:
    """Align the parts on the selection's bounding box, or spread them evenly
    (``xdist``/``ydist``: same gap between neighbours, ends fixed)."""
    parts = [n for n in part_names if comp(data, n) is not None]
    if len(parts) < 2:
        return
    axis = AXES.index(how[0])
    bb = bbox(data, parts)
    if how.endswith("dist"):
        if len(parts) < 3:
            return
        items = sorted(parts, key=lambda n: lo_hi(comp(data, n))[0][axis])
        sizes = [lo_hi(comp(data, n))[1][axis] - lo_hi(comp(data, n))[0][axis] for n in items]
        gap = (bb[1][axis] - bb[0][axis] - sum(sizes)) / (len(items) - 1)
        pos = bb[0][axis]
        for n, s in zip(items, sizes):
            d = [0.0, 0.0, 0.0]
            d[axis] = pos - lo_hi(comp(data, n))[0][axis]
            move(data, [n], d, groups=False)
            pos += s + gap
        return
    for n in parts:
        lo, hi = lo_hi(comp(data, n))
        d = [0.0, 0.0, 0.0]
        if how.endswith("min"):
            d[axis] = bb[0][axis] - lo[axis]
        elif how.endswith("max"):
            d[axis] = bb[1][axis] - hi[axis]
        else:
            d[axis] = (bb[0][axis] + bb[1][axis]) / 2 - (lo[axis] + hi[axis]) / 2
        move(data, [n], d, groups=False)


# --------------------------------------------------------------------- create, copy, delete
def add_part(data: dict, part: dict, kind: str | None = None, on: str | None = None) -> str:
    """Add a part (a component dict, its box size used, its origin ignored), with a
    free name. It is put on top of ``on`` (centred) or on the floor at the middle of
    the enclosure, and shrunk to fit inside the enclosure."""
    p = copy.deepcopy(part)
    p["name"] = unique_name(data, str(p.get("name") or kind or "piece"))
    E = enclosure(data)
    lo, hi = lo_hi(p)
    size = [min(hi[i] - lo[i], E[i]) if E[i] > 0 else hi[i] - lo[i] for i in range(3)]
    target = comp(data, on) if on else None
    if target is not None:
        tlo, thi = lo_hi(target)
        lo = [(tlo[i] + thi[i] - size[i]) / 2 for i in (0, 1)] + [thi[2]]
        if lo[2] + size[2] > E[2] > 0:  # no room above it: back on the floor
            lo[2] = 0.0
    else:
        lo = [(E[i] - size[i]) / 2 for i in (0, 1)] + [0.0]
    set_lo_size(p, lo, size)
    components(data).append(p)
    if target is None:
        lo[2] = drop_height(data, p["name"])
        set_lo_size(p, lo, size)
    if kind:
        meta(data)["kinds"][p["name"]] = kind
    return p["name"]


def _copy_part(data: dict, name: str, delta: list[float], taken: set[str]) -> str:
    src = comp(data, name)
    new = copy.deepcopy(src)
    new["name"] = unique_name(data, name, taken)
    taken.add(new["name"])
    lo, hi = lo_hi(src)
    set_lo_size(new, [lo[i] + delta[i] for i in range(3)], [hi[i] - lo[i] for i in range(3)])
    components(data).append(new)
    kinds = meta(data)["kinds"]
    if name in kinds:
        kinds[new["name"]] = kinds[name]
    for r in list(data.get("refinements") or []):
        if r.get("target") == name:
            data["refinements"].append({**copy.deepcopy(r), "target": new["name"]})
    return new["name"]


def duplicate_offset(data: dict, part_names: list[str]) -> list[float]:
    """Where a copy goes: next to the originals along x, else y, if that stays in
    the enclosure; else a small shift (the overlap alert will say so)."""
    bb = bbox(data, part_names)
    if bb is None:
        return [0.0, 0.0, 0.0]
    E = enclosure(data)
    for axis in (0, 1):
        width = bb[1][axis] - bb[0][axis]
        gap = max(0.002, 0.1 * width)
        if bb[1][axis] + gap + width <= E[axis] + EPS:
            d = [0.0, 0.0, 0.0]
            d[axis] = width + gap
            return d
        if bb[0][axis] - gap - width >= -EPS:
            d = [0.0, 0.0, 0.0]
            d[axis] = -(width + gap)
            return d
    return [0.005, 0.005, 0.0]


def duplicate(data: dict, part_names: list[str], delta: list[float] | None = None) -> list[str]:
    """Copy the parts (whole groups are copied as new groups). Returns the new names."""
    parts = [n for n in with_groups(data, part_names)]
    if not parts:
        return []
    d = duplicate_offset(data, parts) if delta is None else delta
    taken: set[str] = set()
    mapping = {n: _copy_part(data, n, d, taken) for n in parts}
    for g, members in list(read_meta(data)["groups"].items()):
        if all(m in mapping for m in members):
            group(data, [mapping[m] for m in members], unique_name(
                {"components": [{"name": x} for x in meta(data)["groups"]]}, g))
    return list(mapping.values())


def linear_array(data: dict, part_names: list[str], count: int, axis: int,
                 pitch: float) -> list[str]:
    """Make ``count`` - 1 copies of the parts, each ``pitch`` further along ``axis``
    (``count`` is the total number, originals included). Returns the new names."""
    out: list[str] = []
    for k in range(1, max(1, int(count))):
        d = [0.0, 0.0, 0.0]
        d[axis] = k * pitch
        out += duplicate(data, part_names, d)
    return out


def delete(data: dict, part_names: Iterable[str]) -> None:
    gone = set(part_names)
    data["components"] = [c for c in data.get("components") or [] if c.get("name") not in gone]
    if data.get("refinements"):
        data["refinements"] = [r for r in data["refinements"] if r.get("target") not in gone]
    m = meta(data)
    for members in m["groups"].values():
        members[:] = [n for n in members if n not in gone]
    _drop_empty_groups(m)
    m["hidden"] = [n for n in m["hidden"] if n not in gone]
    m["locked"] = [n for n in m["locked"] if n not in gone]
    m["kinds"] = {k: v for k, v in m["kinds"].items() if k not in gone}


def rename_error(data: dict, old: str, new: str) -> str | None:
    """Why ``new`` cannot be the name of part ``old`` (None when it can)."""
    if new == old:
        return None
    if not NAME_RE.match(new or ""):
        return "lettres, chiffres et _ uniquement, en commençant par une lettre"
    if new in taken_names(data):
        return f"le nom « {new} » est déjà pris"
    return None


def rename(data: dict, old: str, new: str) -> None:
    c = comp(data, old)
    if c is None or old == new:
        return
    c["name"] = new
    for r in data.get("refinements") or []:
        if r.get("target") == old:
            r["target"] = new
    m = meta(data)
    for members in m["groups"].values():
        members[:] = [new if n == old else n for n in members]
    for flag in ("hidden", "locked"):
        m[flag] = [new if n == old else n for n in m[flag]]
    if old in m["kinds"]:
        m["kinds"][new] = m["kinds"].pop(old)


# --------------------------------------------------------------------- checks
def overlaps(data: dict) -> list[tuple[str, str, list[float]]]:
    """Pairs of parts whose volumes intersect, with the overlap depth per axis.
    Parts that only touch (stacked, side by side) do not count."""
    parts = [(str(c.get("name")), *lo_hi(c)) for c in data.get("components") or []]
    out = []
    for i, (a, alo, ahi) in enumerate(parts):
        for b, blo, bhi in parts[i + 1:]:
            depth = [min(ahi[k], bhi[k]) - max(alo[k], blo[k]) for k in range(3)]
            if all(v > 1e-7 for v in depth):
                out.append((a, b, depth))
    return out


def outside(data: dict) -> list[tuple[str, int, float]]:
    """Parts sticking out of the enclosure: (name, axis, by how much) for the axis
    where it sticks out most."""
    E = enclosure(data)
    out = []
    for c in data.get("components") or []:
        lo, hi = lo_hi(c)
        worst = max(((i, max(-lo[i], hi[i] - E[i])) for i in range(3)), key=lambda t: t[1])
        if worst[1] > 1e-9:
            out.append((str(c.get("name")), worst[0], worst[1]))
    return out


def total_power(data: dict) -> tuple[float, int]:
    powers = [_f(c.get("power")) for c in data.get("components") or []]
    return sum(p for p in powers if p > 0), sum(1 for p in powers if p > 0)
