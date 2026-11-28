"""3D view: case geometry before the run, temperature / velocity after it.

Owned by: workstream "ui".

The scene content is built by plain functions (:func:`geometry_items`,
:func:`result_items`) so it can be tested without a display. :class:`SceneView`
draws them with pyvistaqt, or shows a placeholder when 3D is unavailable or
disabled with the environment variable ``AIRCONSTRUCT_NO_3D=1`` (used by tests).
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from ..case import Case, Face, OpeningKind

KELVIN = 273.15
OPENING_THICKNESS = 0.002  # fraction of the enclosure diagonal, only for display

FIELDS = {"T": "Température (°C)", "U": "Vitesse (m/s)"}


@dataclass
class SceneItem:
    name: str
    mesh: object  # pyvista.DataSet
    color: str | None = None
    opacity: float = 1.0
    style: str = "surface"  # or "wireframe"
    scalars: str | None = None
    kwargs: dict = field(default_factory=dict)
    key: str | None = None  # selection key (selection.py) of the object it shows
    label: str | None = None  # text drawn next to the item (e.g. "cpu · 68 °C")
    highlight_opacity: float = 0.8  # opacity at least this when selected (surfaces)


def opening_bounds(case: Case, opening) -> tuple[float, float, float, float, float, float]:
    """(xmin, xmax, ymin, ymax, zmin, zmax) of a thin slab showing an opening."""
    E = case.enclosure.as_tuple()
    axis = {"x": 0, "y": 1, "z": 2}[opening.face.value[0]]
    in_plane = [i for i in range(3) if i != axis]
    lo = [0.0, 0.0, 0.0]
    hi = [0.0, 0.0, 0.0]
    for k, i in enumerate(in_plane):
        lo[i] = opening.center[k] - opening.size[k] / 2
        hi[i] = opening.center[k] + opening.size[k] / 2
    t = OPENING_THICKNESS * (E[0] ** 2 + E[1] ** 2 + E[2] ** 2) ** 0.5
    at = 0.0 if opening.face in (Face.xmin, Face.ymin, Face.zmin) else E[axis]
    lo[axis], hi[axis] = at - t / 2, at + t / 2
    return (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])


PASSIVE_COLORS = {"fr4": "#3f7f4f", "aluminium": "#b8c2cc", "copper": "#c87533",
                  "steel": "#7d8790", "plastic_abs": "#d9d4c7", "silicon": "#5b6470"}


def _heat_color(power: float, p_max: float) -> str:
    if power <= 0:
        return "#9aa5b1"
    f = min(1.0, power / p_max) if p_max > 0 else 1.0
    g = int(180 * (1 - f))
    return f"#ff{g:02x}20"


def part_mesh(lo, hi, shape: dict | None = None):
    """What a part looks like: its box, or its imported CAD shape (``shape`` is the
    component's ``shape`` dict) fitted in the box. A shape that cannot be read is
    drawn as the box."""
    import pyvista as pv

    if shape and shape.get("data"):
        try:
            from ..cad import placed

            tris = placed(str(shape["data"]), lo, hi)
        except ValueError:
            tris = None
        if tris is not None and len(tris):
            import numpy as np

            n = len(tris)
            faces = np.hstack([np.full((n, 1), 3), np.arange(3 * n).reshape(n, 3)])
            return pv.PolyData(tris.reshape(-1, 3), faces.ravel()).clean()
    return pv.Box((lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]))


def part_edges(shape: dict | None, color: str = "#333333", width: int = 1) -> dict:
    """add_mesh options for a part: box edges drawn, a CAD shape smooth (its triangle
    edges would hide it)."""
    if shape and shape.get("data"):
        return {"smooth_shading": True, "split_sharp_edges": True}
    return {"show_edges": True, "edge_color": color, "line_width": width}


def geometry_items(case: Case) -> list[SceneItem]:
    """Enclosure outline, components (heat sources warm coloured), openings."""
    import pyvista as pv

    E = case.enclosure.as_tuple()
    items = [SceneItem("enclosure", pv.Box((0, E[0], 0, E[1], 0, E[2])), "#555555",
                       style="wireframe", key="enclosure")]
    p_max = max((c.power for c in case.components), default=0.0)
    for c in case.components:
        lo, hi = c.box.min, c.box.max
        shape = c.shape.model_dump() if c.shape is not None else None
        mesh = part_mesh(lo, hi, shape)
        edges = part_edges(shape)
        if c.power > 0:
            items.append(SceneItem(c.name, mesh, _heat_color(c.power, p_max), kwargs=edges,
                                   key=f"component:{c.name}"))
        else:  # passive part: material colour, see-through so heat sources stay visible
            color = PASSIVE_COLORS.get(c.material, "#9aa5b1")
            opacity = 1.0 if c.material == "fr4" else 0.45
            items.append(SceneItem(c.name, mesh, color, opacity=opacity, kwargs=edges,
                                   key=f"component:{c.name}"))
    for o in case.openings:
        color = "#2f80ed" if o.kind == OpeningKind.inlet else "#27ae60"
        items.append(SceneItem(o.name, pv.Box(opening_bounds(case, o)), color, opacity=0.7,
                               key=f"opening:{o.name}"))
    return items


def _field_values(mesh, name: str):
    """Cell (or point) values for 'T' in degC or 'U' magnitude; None if missing."""
    import numpy as np

    for data in (mesh.cell_data, mesh.point_data):
        if name in data:
            v = np.asarray(data[name])
            if name == "U":
                return np.linalg.norm(v.reshape(len(v), -1), axis=1)
            return v - KELVIN  # OpenFOAM temperatures are Kelvin
    return None


def slice_height(case: Case) -> float:
    """Height of the air slice: through the most powerful component, else mid-height."""
    sources = [c for c in case.components if c.power > 0]
    if not sources:
        return case.enclosure.z / 2
    c = max(sources, key=lambda c: c.power)
    return (c.box.min[2] + c.box.max[2]) / 2


def result_items(case: Case, regions: dict, field_name: str = "T") -> list[SceneItem]:
    """Colour the solids and a mid-height slice of the air by the chosen field."""
    import numpy as np

    items = [i for i in geometry_items(case) if i.name == "enclosure"]
    prepared = {}
    lo, hi = np.inf, -np.inf
    for name, mesh in regions.items():
        vals = _field_values(mesh, field_name)
        if vals is None:
            continue
        m = mesh.copy()
        key = f"{field_name}_display"
        if len(vals) == m.n_cells:
            m.cell_data[key] = vals
        else:
            m.point_data[key] = vals
        prepared[name] = m
        lo, hi = min(lo, float(np.nanmin(vals))), max(hi, float(np.nanmax(vals)))
    if not prepared:
        return geometry_items(case)
    clim = (lo, hi if hi > lo else lo + 1e-6)
    key = f"{field_name}_display"
    for name, m in prepared.items():
        kwargs = {"clim": clim, "cmap": "coolwarm" if field_name == "T" else "viridis",
                  "scalar_bar_args": {"title": FIELDS.get(field_name, field_name)}}
        if name == case.fluid_region:
            z = slice_height(case)
            sl = m.slice(normal="z", origin=(case.enclosure.x / 2, case.enclosure.y / 2, z))
            if sl.n_points:
                items.append(SceneItem(f"{name}_slice", sl, opacity=0.85, scalars=key,
                                       kwargs=kwargs))
        else:
            items.append(SceneItem(name, m, scalars=key, kwargs=kwargs,
                                   key=f"component:{name}"))
    return items


CLICK_PIXELS = 5  # a press and release closer than this is a click, not a drag
DOUBLE_CLICK_S = 0.4


@dataclass
class Pick:
    """A click in the 3D view: the item under the cursor and where it was hit."""

    key: str | None  # selection key of the item clicked, None for none / no key
    point: tuple | None  # world point hit on that item (None: nothing under the cursor)
    origin: tuple  # the ray through the cursor: a point and a unit direction
    direction: tuple
    name: str | None = None  # scene item name
    additive: bool = False  # Ctrl or Shift held
    free: bool = False  # Alt held
    double: bool = False  # second click on the same item
    on_item: bool = True  # False when a VTK widget (move/resize handle) was hit
    normal: tuple | None = None  # outward surface normal at ``point``, when known


def drag_normal(view_dir, vertical: bool = False) -> tuple[float, float, float]:
    """Normal of the plane a drag moves on, from the camera's viewing direction.

    The plane is a main one (the drag changes two coordinates and keeps the third):
    the one most facing the camera, horizontal when looking from above or in the
    iso view. ``vertical`` (Shift held): a vertical plane facing the camera, the drag
    then only changes the height."""
    v = [float(c) for c in view_dir]
    if vertical:
        n = math.hypot(v[0], v[1])
        return (v[0] / n, v[1] / n, 0.0) if n > 1e-9 else (0.0, 1.0, 0.0)
    a = [abs(c) for c in v]
    axis = 2 if a[2] >= 0.8 * max(a[0], a[1]) else (0 if a[0] >= a[1] else 1)
    return tuple(1.0 if i == axis else 0.0 for i in range(3))


def ray_plane(origin, direction, point, normal) -> tuple[float, float, float] | None:
    """Where the ray (origin, direction) meets the plane (point, normal); None if
    parallel or behind."""
    denom = sum(d * n for d, n in zip(direction, normal))
    if abs(denom) < 1e-12:
        return None
    t = sum((p - o) * n for p, o, n in zip(point, origin, normal)) / denom
    if t < 0:
        return None
    return tuple(o + t * d for o, d in zip(origin, direction))


def near_box_edge(bounds, point, tol: float) -> bool:
    """True when ``point`` (on the surface of the box ``bounds``) is within ``tol``
    of one of its twelve edges, i.e. close to two of its faces."""
    close = sum(min(abs(point[i] - bounds[2 * i]), abs(point[i] - bounds[2 * i + 1])) <= tol
                for i in range(3))
    return close >= 2


def _vtk_cell_picker():
    from vtkmodules.vtkRenderingCore import vtkCellPicker

    picker = vtkCellPicker()
    picker.SetTolerance(0.002)
    return picker


def three_d_enabled() -> bool:
    return os.environ.get("AIRCONSTRUCT_NO_3D", "") not in ("1", "true", "yes")


BACKGROUND, BACKGROUND_TOP = "#E7EAEF", "#FFFFFF"  # theme.COLORS background gradient
CAMERAS = {"top": "Dessus", "front": "Face", "side": "Côté", "iso": "Iso"}


class SceneView(QWidget):
    """pyvistaqt viewport, or a text placeholder when 3D is not available.

    ``clicked(Pick)`` is emitted when the user clicks (press and release without
    moving) the scene; the viewport turns it into a selection change. Pressing on an
    item and moving drags it when :attr:`drag_start` accepts it (see the mouse
    section below)."""

    picked = Signal(object, bool)
    clicked = Signal(object)  # Pick

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = None
        self.last_items: list[SceneItem] = []
        self.drag_keys: list[str] = []  # items moved with the mouse during a drag
        if three_d_enabled():
            try:
                from pyvistaqt import QtInteractor

                self.plotter = QtInteractor(self)
                self.plotter.set_background(BACKGROUND, top=BACKGROUND_TOP)
                layout.addWidget(self.plotter.interactor)
                self._enable_picking()
            except Exception as exc:  # noqa: BLE001 - no OpenGL, missing package...
                self.plotter = None
                self._placeholder(f"3D view unavailable: {exc}")
        else:
            self._placeholder("3D view disabled")

    def _placeholder(self, text: str) -> None:
        self.placeholder = QLabel(text)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.layout().addWidget(self.placeholder)

    def draw(self, items: list[SceneItem], reset_camera: bool = False) -> None:
        self.last_items = items
        p = self.plotter
        if p is None:
            self.placeholder.setText(
                "3D view unavailable. Scene: " + ", ".join(i.name for i in items)
            )
            return
        first = not p.renderer.actors
        p.clear()
        for it in items:
            kw = dict(it.kwargs)
            if it.scalars:
                kw["scalars"] = it.scalars
            else:
                kw["color"] = it.color
            p.add_mesh(it.mesh, style=it.style, opacity=it.opacity, name=it.name, **kw)
        labelled = [it for it in items if it.label]
        if labelled:
            import numpy as np

            b = [it.mesh.bounds for it in labelled]
            pts = np.array([((x0 + x1) / 2, (y0 + y1) / 2, z1) for x0, x1, y0, y1, _z0, z1 in b])
            p.add_point_labels(pts, [it.label for it in labelled], name="labels",
                               font_size=12, show_points=False, shape_opacity=0.9,
                               shape_color="white", text_color="#111827",
                               always_visible=True, pickable=False, reset_camera=False)
        p.add_axes()
        if first or reset_camera:
            p.view_isometric()
            p.reset_camera()
        p.render()

    # ------------------------------------------------------------------ mouse
    # A click (press and release without moving) picks the item under the cursor:
    # ``clicked`` then ``picked``. A press on an item that :attr:`drag_start` accepts
    # starts a drag instead of turning the camera: the mouse moves on a plane through
    # the grabbed point (:func:`drag_normal`), the items of the drag follow it
    # (:meth:`preview_offset`) and :attr:`drag_end` gets the move when released.
    # Everything else (turning, panning, zooming, the move/resize handles) is left
    # to VTK.

    #: called on a press over an item: (key, point) -> bool (True = drag it)
    drag_start: Callable[[str, tuple], bool] | None = None
    #: called while dragging: (delta, free) -> delta actually shown
    drag_move: Callable[[tuple, bool], tuple] | None = None
    #: called on release: (delta, free) -> bool (True = the model changed, a redraw follows)
    drag_end: Callable[[tuple, bool], bool] | None = None
    def _enable_picking(self) -> None:
        # Qt sees the mouse before VTK: an event this filter keeps never reaches the
        # camera style or the box widgets
        self._picker = _vtk_cell_picker()
        self._press = None  # (x, y, pick) of the last left press, display pixels
        self._drag = None  # state of the drag in progress
        self._last_click = (0.0, None)
        self.plotter.interactor.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:
        p = self.plotter
        if p is None or obj is not p.interactor:
            return False
        t = ev.type()
        try:
            if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
                return self._on_press(*self._display_xy(ev))
            if t == QEvent.Type.MouseMove and self._drag is not None:
                return self._on_move(*self._display_xy(ev))
            if t == QEvent.Type.MouseButtonRelease and ev.button() == Qt.MouseButton.LeftButton:
                return self._on_release(*self._display_xy(ev))
        except Exception:  # noqa: BLE001 - never break the camera
            self._drag = None
        return False

    def _display_xy(self, ev) -> tuple[int, int]:
        """Qt widget position -> VTK display pixels (origin bottom left)."""
        w = self.plotter.interactor
        r = w.devicePixelRatioF()
        pos = ev.position()
        return round(pos.x() * r), round((w.height() - pos.y() - 1) * r)

    def _mods(self) -> tuple[bool, bool, bool]:
        """(additive: Ctrl or Shift, vertical: Shift, free: Alt)."""
        mods = QApplication.keyboardModifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        return (shift or ctrl, shift, bool(mods & Qt.KeyboardModifier.AltModifier))

    def pick_at(self, x: int, y: int) -> Pick:
        """What is under the display position (x, y): the item, the point, the ray.

        A wireframe item (the enclosure outline) only counts when the cursor is on
        one of its lines: clicking inside the box reaches the parts behind it."""
        origin, direction = self._ray(x, y)
        r = self.plotter.renderer
        actors = r.actors  # held while picking: VTK then hands back these same wrappers
        items = {i.name: i for i in self.last_items}
        skipped = []  # wireframe actors hit away from their lines, made unpickable
        try:
            for _ in range(8):
                if not self._picker.Pick(x, y, 0, r):
                    return Pick(None, None, origin, direction)
                actor = self._picker.GetActor() or self._picker.GetViewProp()
                name = next((n for n, a in actors.items() if a is actor), None)
                item = items.get(name)
                point = tuple(float(v) for v in self._picker.GetPickPosition())
                if item is not None and item.style == "wireframe" and actor is not None \
                        and not near_box_edge(item.mesh.bounds, point, self._edge_tolerance()):
                    actor.SetPickable(False)
                    skipped.append(actor)
                    continue
                normal = tuple(float(v) for v in self._picker.GetPickNormal())
                return Pick(item.key if item else None, point, origin, direction, name=name,
                            on_item=item is not None, normal=normal)
            return Pick(None, None, origin, direction)
        finally:
            for a in skipped:
                a.SetPickable(True)

    def _edge_tolerance(self) -> float:
        b = self.plotter.renderer.ComputeVisiblePropBounds()
        return 0.01 * math.sqrt(sum((b[2 * i + 1] - b[2 * i]) ** 2 for i in range(3)))

    def _ray(self, x: float, y: float) -> tuple[tuple, tuple]:
        r = self.plotter.renderer
        ends = []
        for z in (0.0, 1.0):
            r.SetDisplayPoint(x, y, z)
            r.DisplayToWorld()
            w = r.GetWorldPoint()
            ends.append(tuple(w[i] / (w[3] or 1.0) for i in range(3)))
        d = tuple(b - a for a, b in zip(*ends))
        n = math.sqrt(sum(v * v for v in d)) or 1.0
        return ends[0], tuple(v / n for v in d)

    def _on_press(self, x: int, y: int) -> bool:
        """True when the press starts a drag (kept from VTK)."""
        self._drag = None
        pick = self.pick_at(x, y)
        self._press = (x, y, pick)
        if pick.key is None or pick.point is None or self.drag_start is None:
            return False
        _add, vertical, _free = self._mods()
        if not self.drag_start(pick.key, pick.point):
            return False
        view = self.plotter.camera.GetDirectionOfProjection()
        self._drag = {"origin": pick.point, "normal": drag_normal(view, vertical),
                      "vertical": vertical, "delta": (0.0, 0.0, 0.0), "moved": False}
        return True

    def _on_move(self, x: int, y: int) -> bool:
        d = self._drag
        px, py, _ = self._press
        if not d["moved"] and abs(x - px) + abs(y - py) < CLICK_PIXELS:
            return True
        d["moved"] = True
        origin, direction = self._ray(x, y)
        hit = ray_plane(origin, direction, d["origin"], d["normal"])
        if hit is None:
            return True
        raw = tuple(h - o for h, o in zip(hit, d["origin"]))
        if d["vertical"]:
            raw = (0.0, 0.0, raw[2])
        _add, _v, free = self._mods()
        shown = self.drag_move(raw, free) if self.drag_move else raw
        d["delta"] = raw
        self.preview_offset(self.drag_keys, shown)
        return True

    def _on_release(self, x: int, y: int) -> bool:
        """A click (no move) is emitted; a drag is finished. True when it was a drag."""
        d, press = self._drag, self._press
        self._drag = self._press = None
        additive, _v, free = self._mods()
        if d is not None:
            changed = False
            if self.drag_end is not None:
                changed = bool(self.drag_end(d["delta"] if d["moved"] else (0.0, 0.0, 0.0),
                                             free))
            if not changed:
                self.preview_offset(self.drag_keys, (0.0, 0.0, 0.0))
            self.drag_keys = []
            if d["moved"]:
                return True
        if press is None:
            return d is not None
        px, py, pick = press
        if abs(x - px) + abs(y - py) >= CLICK_PIXELS or not pick.on_item:
            return d is not None  # the camera was turned, or a handle was used
        now = time.monotonic()
        t, key = self._last_click
        pick.double = key == pick.key and now - t < DOUBLE_CLICK_S
        self._last_click = (0.0, None) if pick.double else (now, pick.key)
        pick.additive, pick.free = additive, free
        self.click(pick)
        return d is not None

    def click(self, pick: Pick) -> None:
        """Emit ``clicked`` and ``picked`` for a click (also used by tests)."""
        self.clicked.emit(pick)
        self.picked.emit(pick.key, pick.additive)

    def pick_name(self, name: str | None, additive: bool = False) -> None:
        """A click on the scene item called ``name`` (used by tests)."""
        item = next((i for i in self.last_items if i.name == name), None)
        point = None
        if item is not None:
            b = item.mesh.bounds
            point = ((b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2)
        self.click(Pick(item.key if item else None, point, (0.0, 0.0, 1e3), (0.0, 0.0, -1.0),
                        name=name, additive=additive))

    def preview_offset(self, keys, delta) -> None:
        """Draw the items with these selection keys moved by ``delta`` (a drag)."""
        p = self.plotter
        if p is None:
            return
        wanted = set(keys)
        names = {i.name for i in self.last_items if i.key in wanted}
        for name, actor in p.renderer.actors.items():
            if name in names:
                actor.SetPosition(*delta)
        p.render()

    def set_camera(self, view: str) -> None:
        """One of CAMERAS: top (looking down -z), front (-y), side (+x), iso."""
        p = self.plotter
        if p is None:
            return
        {"top": p.view_xy, "front": p.view_xz, "side": p.view_yz,
         "iso": p.view_isometric}[view]()
        p.reset_camera()
        p.render()

    def frame_bounds(self, bounds) -> None:
        """Point the camera at ``bounds`` (xmin, xmax, ymin, ymax, zmin, zmax)."""
        p = self.plotter
        if p is None:
            return
        x0, x1, y0, y1, z0, z1 = bounds
        pad = 0.6 * max(x1 - x0, y1 - y0, z1 - z0, 1e-9)
        p.reset_camera(bounds=(x0 - pad, x1 + pad, y0 - pad, y1 + pad, z0 - pad, z1 + pad))
        p.set_focus(((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))
        p.render()

    def show_case(self, case: Case, reset_camera: bool = False) -> None:
        self.draw(geometry_items(case), reset_camera)

    def show_results(self, case: Case, regions: dict, field_name: str = "T") -> None:
        self.draw(result_items(case, regions, field_name))

    def close_plotter(self) -> None:
        if self.plotter is not None:
            self.plotter.close()
            self.plotter = None
