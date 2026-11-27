"""3D view: case geometry before the run, temperature / velocity after it.

Owned by: workstream "ui".

The scene content is built by plain functions (:func:`geometry_items`,
:func:`result_items`) so it can be tested without a display. :class:`SceneView`
draws them with pyvistaqt, or shows a placeholder when 3D is unavailable or
disabled with the environment variable ``AIRCONSTRUCT_NO_3D=1`` (used by tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
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


def geometry_items(case: Case) -> list[SceneItem]:
    """Enclosure outline, component boxes (heat sources warm coloured), openings."""
    import pyvista as pv

    E = case.enclosure.as_tuple()
    items = [SceneItem("enclosure", pv.Box((0, E[0], 0, E[1], 0, E[2])), "#555555",
                       style="wireframe", key="enclosure")]
    p_max = max((c.power for c in case.components), default=0.0)
    for c in case.components:
        lo, hi = c.box.min, c.box.max
        mesh = pv.Box((lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]))
        edges = {"show_edges": True, "edge_color": "#333333", "line_width": 1}
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


def three_d_enabled() -> bool:
    return os.environ.get("AIRCONSTRUCT_NO_3D", "") not in ("1", "true", "yes")


BACKGROUND, BACKGROUND_TOP = "#E7EAEF", "#FFFFFF"  # theme.COLORS background gradient
CAMERAS = {"top": "Dessus", "front": "Face", "side": "Côté", "iso": "Iso"}


class SceneView(QWidget):
    """pyvistaqt viewport, or a text placeholder when 3D is not available.

    ``picked(key, additive)`` is emitted when the user clicks an object (key is the
    item's selection key, additive when Ctrl or Shift is held)."""

    picked = Signal(object, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = None
        self.last_items: list[SceneItem] = []
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
        p.add_axes()
        if first or reset_camera:
            p.view_isometric()
            p.reset_camera()
        p.render()

    def _enable_picking(self) -> None:
        try:
            self.plotter.enable_mesh_picking(self._on_pick_actor, use_actor=True, show=False,
                                             show_message=False, left_clicking=True)
        except Exception:  # noqa: BLE001, S110 - picking is a convenience
            pass

    def _on_pick_actor(self, actor) -> None:
        actors = self.plotter.renderer.actors if self.plotter is not None else {}
        name = next((n for n, a in actors.items() if a is actor), None)
        mods = QApplication.keyboardModifiers()
        additive = bool(mods & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.ShiftModifier))
        self.pick_name(name, additive)

    def pick_name(self, name: str | None, additive: bool = False) -> None:
        """Emit ``picked`` for the scene item called ``name`` (also used by tests)."""
        item = next((i for i in self.last_items if i.name == name), None)
        self.picked.emit(item.key if item else None, additive)

    def set_camera(self, view: str) -> None:
        """One of CAMERAS: top (looking down -z), front (-y), side (+x), iso."""
        p = self.plotter
        if p is None:
            return
        {"top": p.view_xy, "front": p.view_xz, "side": p.view_yz,
         "iso": p.view_isometric}[view]()
        p.reset_camera()
        p.render()

    def show_case(self, case: Case, reset_camera: bool = False) -> None:
        self.draw(geometry_items(case), reset_camera)

    def show_results(self, case: Case, regions: dict, field_name: str = "T") -> None:
        self.draw(result_items(case, regions, field_name))

    def close_plotter(self) -> None:
        if self.plotter is not None:
            self.plotter.close()
            self.plotter = None
