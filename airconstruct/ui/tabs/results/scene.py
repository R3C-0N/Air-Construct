"""3D content of the Résultats tab: a pipeline of filters (parts, cut planes,
isosurface, vectors, streamlines, particles, A→B line) turned into ``SceneItem``s.

Owned by: thread "Résultats" (see docs/ui-architecture.md).

Everything here is plain pyvista + numpy (no widgets), so it is tested offscreen.
:class:`FieldData` wraps the regions of one run and caches what is slow to build
(point data, streamlines).
"""

from __future__ import annotations

import copy
import math
import warnings
from dataclasses import dataclass, field

import numpy as np

from ....case import Case, Face, OpeningKind
from ...view import KELVIN, SceneItem, geometry_items

FIELD_LABELS = {"T": "Température (°C)", "U": "Vitesse (m/s)"}
CMAPS = {"T": "coolwarm", "U": "viridis"}
AXES = {"x": 0, "y": 1, "z": 2}
DISPLAY = "display"  # point-data array holding the shown field (°C or m/s)

# filter kinds: title, default parameters
KINDS: dict[str, tuple[str, dict]] = {
    "parts": ("Pièces", {"opacity": 1.0}),
    "slice": ("Coupe", {"axis": "y", "position": None}),
    "iso": ("Isosurface", {"value": None, "opacity": 0.45}),
    "vectors": ("Vecteurs", {"axis": "z", "position": None, "density": 18, "scale": 1.0}),
    "streamlines": ("Lignes de courant", {"seeds": 36, "thickness": 1.0}),
    "particles": ("Particules", {"count": 160}),
    "line": ("Ligne de mesure", {"a": None, "b": None}),
}
DEFAULT_PIPELINE = ("parts", "slice", "streamlines")


@dataclass
class Filter:
    kind: str
    visible: bool = True
    params: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        defaults = copy.deepcopy(KINDS[self.kind][1])
        defaults.update(self.params)
        self.params = defaults

    @property
    def title(self) -> str:
        t = KINDS[self.kind][0]
        if self.kind in ("slice", "vectors"):
            t += " " + {"x": "verticale (x)", "y": "verticale (y)", "z": "horizontale"}[
                self.params["axis"]]
        return t

    def to_dict(self) -> dict:
        return {"kind": self.kind, "visible": self.visible, "params": self.params}

    @classmethod
    def from_dict(cls, d: dict) -> Filter | None:
        if not isinstance(d, dict) or d.get("kind") not in KINDS:
            return None
        return cls(d["kind"], bool(d.get("visible", True)), dict(d.get("params") or {}))


def default_pipeline() -> list[Filter]:
    return [Filter(k) for k in DEFAULT_PIPELINE]


# --------------------------------------------------------------------------- data


def _values(mesh, name: str) -> np.ndarray | None:
    """Point values of 'T' (°C) or 'U' (magnitude, m/s), None when missing."""
    if name not in mesh.point_data:
        return None
    v = np.asarray(mesh.point_data[name], dtype=float)
    if name == "U":
        return np.linalg.norm(v.reshape(len(v), -1), axis=1)
    return v - KELVIN


class FieldData:
    """The regions of one run, with the chosen field as point data ``display``."""

    def __init__(self, case: Case, regions: dict) -> None:
        self.case = case
        self.regions = regions
        self._points: dict[str, object] = {}
        self._prepared: dict[str, dict] = {}
        self._streamlines: dict[int, object] = {}

    @property
    def fluid(self) -> str:
        return self.case.fluid_region

    def point_mesh(self, region: str):
        """Region mesh with point data (cell data interpolated when needed)."""
        if region not in self._points:
            m = self.regions[region]
            if not any(k in m.point_data for k in ("T", "U")):
                m = m.cell_data_to_point_data()
            self._points[region] = m
        return self._points[region]

    def prepared(self, fld: str) -> dict:
        """{region: mesh copy with point array DISPLAY} for regions that have ``fld``."""
        if fld not in self._prepared:
            out = {}
            for name in self.regions:
                m = self.point_mesh(name)
                vals = _values(m, fld)
                if vals is None:
                    continue
                m = m.copy(deep=False)
                m.point_data[DISPLAY] = vals
                if "U" in m.point_data:
                    m.point_data["U"] = np.asarray(m.point_data["U"])
                    m.set_active_vectors("U")
                out[name] = m
            self._prepared[fld] = out
        return self._prepared[fld]

    def range(self, fld: str, region: str | None = None) -> tuple[float, float]:
        """(min, max) of the field over every region, or over one region."""
        vals = [np.asarray(m.point_data[DISPLAY]) for n, m in self.prepared(fld).items()
                if region is None or n == region]
        vals = [v[np.isfinite(v)] for v in vals if len(v)]
        vals = [v for v in vals if len(v)]
        if not vals:
            return (0.0, 1.0)
        lo, hi = float(min(v.min() for v in vals)), float(max(v.max() for v in vals))
        return (lo, hi if hi > lo else lo + 1e-6)

    # ---- streamlines ------------------------------------------------------
    def streamlines(self, seeds: int):
        """Streamlines from the inlets (fields T and speed as point data), or None."""
        if seeds not in self._streamlines:
            self._streamlines[seeds] = self._make_streamlines(seeds)
        return self._streamlines[seeds]

    def _make_streamlines(self, n: int):
        import pyvista as pv

        air = self.point_mesh(self.fluid) if self.fluid in self.regions else None
        if air is None or "U" not in air.point_data:
            return None
        pts = seed_points(self.case, n)
        if not len(pts):
            return None
        g = air.copy(deep=False)
        g.set_active_vectors("U")
        E = self.case.enclosure.as_tuple()
        try:
            lines = g.streamlines_from_source(pv.PolyData(pts), vectors="U",
                                              max_length=10 * max(E),
                                              integration_direction="forward")
        except Exception:  # noqa: BLE001 - VTK may refuse odd meshes
            return None
        if not lines.n_points:
            return None
        for fld in ("T", "U"):
            v = _values(lines, fld)
            if v is not None:
                lines.point_data[f"{fld}_display"] = v
        return lines


def _opening_frame(case: Case, o) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Centre, the two in-plane unit axes and the inward normal of an opening."""
    E = case.enclosure.as_tuple()
    axis = AXES[o.face.value[0]]
    in_plane = [i for i in range(3) if i != axis]
    c = np.zeros(3)
    c[in_plane[0]], c[in_plane[1]] = o.center
    low = o.face in (Face.xmin, Face.ymin, Face.zmin)
    c[axis] = 0.0 if low else E[axis]
    u, v, n = np.zeros(3), np.zeros(3), np.zeros(3)
    u[in_plane[0]] = 1.0
    v[in_plane[1]] = 1.0
    n[axis] = 1.0 if low else -1.0
    return c, u, v, n


def seed_points(case: Case, n: int) -> np.ndarray:
    """About ``n`` seeds spread over the inlets, just inside the enclosure."""
    ops = [o for o in case.openings if o.kind == OpeningKind.inlet] or list(case.openings)
    if not ops or n <= 0:
        return np.zeros((0, 3))
    E = case.enclosure.as_tuple()
    eps = 1e-3 * max(E)
    per = max(1, n // len(ops))
    out = []
    for o in ops:
        c, u, v, nrm = _opening_frame(case, o)
        su, sv = o.size
        k = max(1, round(math.sqrt(per * su / sv))) if sv > 0 else 1
        m = max(1, math.ceil(per / k))
        for i in range(k):
            for j in range(m):
                a = ((i + 0.5) / k - 0.5) * su
                b = ((j + 0.5) / m - 0.5) * sv
                out.append(c + a * u + b * v + eps * nrm)
    return np.array(out)


def default_line(case: Case) -> tuple[list[float], list[float]]:
    """A→B: from the first inlet to the first outlet (centre of the box otherwise)."""
    E = np.array(case.enclosure.as_tuple())
    ins = [o for o in case.openings if o.kind == OpeningKind.inlet]
    outs = [o for o in case.openings if o.kind != OpeningKind.inlet]
    eps = 0.02 * E.max()
    a = _opening_frame(case, ins[0]) if ins else None
    b = _opening_frame(case, outs[0]) if outs else None
    pa = a[0] + eps * a[3] if a else np.array([0.05, 0.5, 0.5]) * E
    pb = b[0] + eps * b[3] if b else np.array([0.95, 0.5, 0.5]) * E
    return [float(x) for x in pa], [float(x) for x in pb]


def sample_line(data: FieldData, a, b, fld: str = "T", n: int = 200):
    """Values of ``fld`` along A→B through every region: (distance [m], values)."""
    import pyvista as pv

    a, b = np.asarray(a, float), np.asarray(b, float)
    line = pv.Line(a, b, resolution=max(2, n - 1))
    vals = np.full(line.n_points, np.nan)
    for mesh in data.prepared(fld).values():
        s = line.sample(mesh)
        ok = np.asarray(s.point_data.get("vtkValidPointMask", np.zeros(line.n_points))) > 0
        got = np.asarray(s.point_data.get(DISPLAY, np.full(line.n_points, np.nan)))
        take = ok & np.isnan(vals)
        vals[take] = got[take]
    dist = np.linalg.norm(np.asarray(line.points) - a, axis=1)
    return dist, vals


def particle_positions(lines, count: int, phase: float) -> np.ndarray:
    """``count`` tracers spread over the streamlines, moved along by ``phase`` (0..1)."""
    if lines is None or not lines.n_lines or count <= 0:
        return np.zeros((0, 3))
    pts = np.asarray(lines.points)
    conn = np.asarray(lines.lines)
    polylines, i = [], 0
    while i < len(conn):
        k = int(conn[i])
        idx = conn[i + 1:i + 1 + k]
        if k >= 2:
            polylines.append(pts[idx])
        i += k + 1
    if not polylines:
        return np.zeros((0, 3))
    out = []
    per = max(1, count // len(polylines))
    for li, pl in enumerate(polylines):
        seg = np.linalg.norm(np.diff(pl, axis=0), axis=1)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        if s[-1] <= 0:
            continue
        offs = (np.arange(per) / per + phase + 0.37 * li) % 1.0
        t = offs * s[-1]
        out.append(np.column_stack([np.interp(t, s, pl[:, d]) for d in range(3)]))
    return np.concatenate(out) if out else np.zeros((0, 3))


# --------------------------------------------------------------------------- scene


def _surface(mesh):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return mesh.extract_surface()


def _position(case: Case, params: dict, axis: int) -> float:
    E = case.enclosure.as_tuple()
    p = params.get("position")
    return float(p) if p is not None else E[axis] / 2


def _colored(name: str, mesh, fld: str, clim, **kw) -> SceneItem:
    kwargs = {"clim": clim, "cmap": CMAPS[fld],
              "scalar_bar_args": {"title": FIELD_LABELS[fld]}}
    kwargs.update(kw.pop("kwargs", {}))
    return SceneItem(name, mesh, scalars=DISPLAY if DISPLAY in mesh.point_data else
                     f"{fld}_display", kwargs=kwargs, **kw)


def filter_items(data: FieldData, f: Filter, fld: str, clim, phase: float = 0.0
                 ) -> list[SceneItem]:
    """The scene items of one filter (empty when it cannot be drawn)."""
    import pyvista as pv

    case = data.case
    prep = data.prepared(fld)
    air = prep.get(data.fluid)
    E = case.enclosure.as_tuple()
    tag = f"{f.kind}{id(f) % 100000}"
    items: list[SceneItem] = []
    if f.kind == "parts":
        all_t = data.prepared("T")
        for name in data.regions:
            if name == data.fluid:
                continue
            key = f"component:{name}"
            if name in prep:
                surf = _surface(prep[name])
                items.append(_colored(f"part:{name}", surf, fld, clim,
                                      opacity=f.params["opacity"], key=key))
            elif name in all_t:  # e.g. speed shown: solids in grey
                items.append(SceneItem(f"part:{name}", _surface(all_t[name]),
                                       "#9AA5B1", opacity=f.params["opacity"], key=key))
    elif f.kind == "slice":
        ax = f.params["axis"]
        origin = [E[0] / 2, E[1] / 2, E[2] / 2]
        origin[AXES[ax]] = _position(case, f.params, AXES[ax])
        for name, m in prep.items():
            sl = m.slice(normal=ax, origin=origin)
            if sl.n_points:
                items.append(_colored(f"{tag}:{name}", sl, fld, clim, opacity=0.9))
    elif f.kind == "iso" and air is not None:
        lo, hi = data.range(fld, data.fluid)
        v = f.params.get("value")
        v = lo + 0.6 * (hi - lo) if v is None else float(v)
        if lo < v < hi:
            iso = air.contour([v], scalars=DISPLAY)
            if iso.n_points:
                items.append(_colored(tag, iso, fld, clim, opacity=f.params["opacity"]))
    elif f.kind == "vectors":
        u_air = data.prepared("U").get(data.fluid)
        if u_air is not None:
            ax = AXES[f.params["axis"]]
            n = max(2, int(f.params["density"]))
            others = [i for i in range(3) if i != ax]
            g = [np.linspace(0.02 * E[i], 0.98 * E[i], n) for i in others]
            A, B = np.meshgrid(*g, indexing="ij")
            pts = np.zeros((A.size, 3))
            pts[:, others[0]], pts[:, others[1]] = A.ravel(), B.ravel()
            pts[:, ax] = _position(case, f.params, ax)
            s = pv.PolyData(pts).sample(u_air)
            ok = np.asarray(s.point_data.get("vtkValidPointMask", np.ones(len(pts)))) > 0
            s = s.extract_points(ok, adjacent_cells=False) if not ok.all() else s
            if s.n_points and "U" in s.point_data:
                speed = np.linalg.norm(np.asarray(s.point_data["U"]), axis=1)
                vmax = float(speed.max()) if len(speed) else 0.0
                if vmax > 0:
                    s.point_data["U_display"] = speed
                    s.set_active_vectors("U")
                    step = min(E[i] for i in others) / n
                    arrows = s.glyph(orient="U", scale="U_display",
                                     factor=1.4 * step * f.params["scale"] / vmax,
                                     geom=pv.Arrow(tip_resolution=10, shaft_resolution=6))
                    vrange = data.range("U")
                    items.append(SceneItem(tag, arrows, scalars="U_display", kwargs={
                        "clim": clim if fld == "U" else vrange, "cmap": CMAPS["U"],
                        "scalar_bar_args": {"title": FIELD_LABELS["U"]},
                        "show_scalar_bar": fld == "U"}))
    elif f.kind == "streamlines":
        lines = data.streamlines(int(f.params["seeds"]))
        if lines is not None:
            r = 0.002 * max(E) * f.params["thickness"]
            mesh = lines.tube(radius=r) if lines.n_lines else lines
            items.append(_colored(tag, mesh, fld, clim))
    elif f.kind == "particles":
        lines = data.streamlines(int(KINDS["streamlines"][1]["seeds"]))
        pts = particle_positions(lines, int(f.params["count"]), phase)
        if len(pts):
            items.append(SceneItem(PARTICLES, pv.PolyData(pts), "#111827", kwargs={
                "render_points_as_spheres": True, "point_size": 6}))
    elif f.kind == "line":
        a, b = f.params.get("a"), f.params.get("b")
        if a is None or b is None:
            a, b = default_line(case)
        items.append(SceneItem(f"{tag}:ab", pv.Line(a, b), "#111827",
                               kwargs={"line_width": 3}))
        r = 0.012 * max(E)
        items.append(SceneItem(f"{tag}:A", pv.Sphere(radius=r, center=a), "#1D4ED8"))
        items.append(SceneItem(f"{tag}:B", pv.Sphere(radius=r, center=b), "#1D4ED8"))
    return items


PARTICLES = "particles"


def context_items(case: Case) -> list[SceneItem]:
    """Enclosure outline and see-through openings."""
    return [dataclasses_replace(i, opacity=min(i.opacity, 0.35))
            if i.key and i.key.startswith("opening:") else i
            for i in geometry_items(case)
            if i.key == "enclosure" or (i.key or "").startswith("opening:")]


def dataclasses_replace(item: SceneItem, **kw) -> SceneItem:
    import dataclasses

    return dataclasses.replace(item, **kw)


def build_scene(data: FieldData, filters: list[Filter], fld: str, clim,
                phase: float = 0.0) -> list[SceneItem]:
    items = context_items(data.case)
    for f in filters:
        if f.visible:
            items.extend(filter_items(data, f, fld, clim, phase))
    return items


def shifted(items: list[SceneItem], dx: float, suffix: str) -> list[SceneItem]:
    """Copies of ``items`` moved by ``dx`` along x (second run of a comparison)."""
    out = []
    for it in items:
        m = it.mesh.copy()
        m.translate((dx, 0.0, 0.0), inplace=True)
        out.append(dataclasses_replace(it, name=f"{it.name}{suffix}", mesh=m, key=None))
    return out


def difference(a: FieldData, b: FieldData, fld: str) -> FieldData | None:
    """Run B minus run A, drawn on A's meshes; None unless both runs have the same meshes."""
    pa, pb = a.prepared(fld), b.prepared(fld)
    prepared = {}
    for name, ma in pa.items():
        mb = pb.get(name)
        if mb is None or mb.n_points != ma.n_points:
            return None
        m = ma.copy(deep=False)
        m.point_data[DISPLAY] = (np.asarray(mb.point_data[DISPLAY])
                                 - np.asarray(ma.point_data[DISPLAY]))
        prepared[name] = m
    if not prepared:
        return None
    out = FieldData(a.case, a.regions)
    out._prepared[fld] = prepared
    return out
