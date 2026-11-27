"""Step 2, Air: openings (inlets, outlets, fans, grilles), enclosure walls, ambient
conditions, and the quick estimate.

Owned by: thread "Air" (see docs/ui-architecture.md).

- Left panel: ambient conditions, the openings, the walls, the live quick estimate.
- 3D view: the faces of the box can be clicked. A face selected (``"face:<face>"``)
  opens its 2D editor; with a creation tool armed (Entrée I, Sortie O, Ventilateur N)
  the click adds the opening on that face.
- Right panel: the inspector of the selection (opening, face, or walls).
"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...selection import ENCLOSURE, opening_key, parse_key
from ...theme import set_prop
from ...widgets.card import Card
from ...widgets.object_tree import ObjectTree
from ..base import StepTab
from . import ops
from .checks import air_alerts
from .fields import form, muted, section, set_value, spin
from .geometry import FACE_LABELS, NAME_RE, clamp_center, face_size, used_names
from .inspector import FaceInspector, OpeningInspector
from .scene import FACE_PREFIX, WALLS_KEY, air_items, face_key
from .walls import WallsInspector, walls_summary

TOOLS = {  # key: (label, shortcut, tooltip)
    "select": ("Sélection", "V", "Sélection (V)"),
    "inlet": ("+ Entrée", "I", "Entrée d’air : cliquez ensuite sur une face (I)"),
    "outlet": ("+ Sortie", "O", "Sortie d’air : cliquez ensuite sur une face (O)"),
    "fan": ("+ Ventilateur", "N", "Ventilateur : cliquez ensuite sur une face (N)"),
}


def _fr(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


class AirTree(ObjectTree):
    """Openings with what drives them ("11,5 m³/h", "ventilateur · grille 60 %"), then
    the enclosure walls."""

    def rebuild(self) -> None:
        data = self.ctx.project.get() or {}
        h = self.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._syncing = True
        try:
            self.clear()
            self._items = {}
            group = self._add(self, "Ouvertures", None)
            group.setFirstColumnSpanned(True)
            for o in data.get("openings") or []:
                name = str(o.get("name"))
                item = self._add(group, name, opening_key(name), opening_detail(o),
                                 "#2f80ed" if o.get("kind") == "inlet" else "#27ae60")
                kind = "entrée" if o.get("kind") == "inlet" else "sortie"
                item.setToolTip(0, f"{kind} · {FACE_LABELS.get(o.get('face'), '')}")
            group.setExpanded(True)
            group = self._add(self, "Parois", None)
            group.setFirstColumnSpanned(True)
            item = self._add(group, "Boîtier", WALLS_KEY, walls_summary(data), "#9aa5b1")
            item.setToolTip(1, walls_summary(data))
            group.setExpanded(True)
        finally:
            self._syncing = False
        self._selection_to_tree()


def opening_detail(o: dict) -> str:
    q = ops.imposed_flow(o)
    if ops.drive_mode(o) == "fan":
        text = "ventilateur"
    elif q is not None:
        text = f"{_fr(q * ops.M3H)} m³/h"
    else:
        text = "libre"
    g = o.get("grille") or None
    if g:
        oa = g.get("open_area")
        text += (f" · grille {float(oa) * 100:.0f} %" if oa and g.get("loss_coefficient") is None
                 else " · grille")
    return text


class EstimatePanel(QWidget):
    """Quick estimate, refreshed while editing: flow, pressure drop, fans, one temperature
    per part (lumped model, no CFD)."""

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(section("Estimation rapide"), 1)
        live = QLabel("en direct")
        set_prop(live, role="pill", tone="info")
        head.addWidget(live)
        lay.addLayout(head)
        self.flow = muted()
        lay.addWidget(self.flow)
        self.grid = form()
        lay.addLayout(self.grid)
        self.note = muted("Modèle simplifié (une température par pièce) : un ordre de grandeur, "
                          "le calcul donne les vraies valeurs.")
        lay.addWidget(self.note)
        self.values: dict[str, float] = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.update_now)
        ctx.project.changed.connect(self.timer.start)
        ctx.units.changed.connect(self.update_now)

    def update_now(self) -> None:
        self.timer.stop()
        while self.grid.rowCount():
            self.grid.removeRow(0)
        case = self.ctx.project.case
        self.values = {}
        if case is None:
            self.flow.setText("Corrigez les alertes pour obtenir une estimation.")
            return
        from ....fans import estimate_flow
        from ....materials import GASES

        u = self.ctx.units
        try:
            est = estimate_flow(case)
            text = (f"Débit {_fr(est.flow_rate * ops.M3H)} m³/h · perte de charge "
                    f"{_fr(est.pressure_drop)} Pa")
            power = sum(c.power for c in case.components)
            if est.flow_rate > 0 and power > 0:
                from ....fans import air_density

                dt = power / (air_density(case) * est.flow_rate * GASES[case.fluid].cp)
                text += f" · air +{_fr(dt)} °C en sortie"
            for f in est.fans:
                text += f"\n{f.name} : {_fr(f.flow_rate * ops.M3H)} m³/h sous {_fr(f.pressure)} Pa"
            self.flow.setText(text)
        except Exception as exc:  # noqa: BLE001
            self.flow.setText(f"Débit indisponible : {exc}")
        try:
            temps = self.ctx.services.quick_temperatures(case)
        except Exception as exc:  # noqa: BLE001
            self.grid.addRow(muted(f"Estimation indisponible : {exc}"))
            return
        self.values = dict(temps)
        hot = max(temps.values(), default=None)
        for name, t in sorted(temps.items(), key=lambda kv: -kv[1]):
            value = QLabel(u.fmt_temperature(t, 0))
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            if t == hot:
                value.setStyleSheet("font-weight: 700;")
            self.grid.addRow(name, value)


class AirTab(StepTab):
    key = "air"
    title = "Air"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.armed: str | None = None
        self._build_left()
        self._build_right()
        self._build_toolbar()
        self._build_actions()
        ctx.project.changed.connect(self._on_project_changed)
        ctx.selection.changed.connect(self._on_selection_changed)
        ctx.units.changed.connect(self._refresh_conditions)
        self._on_project_changed()
        self._on_selection()
        self.estimate.update_now()

    # ------------------------------------------------------------------ panels
    def _build_left(self) -> None:
        self._left = Card("Air")
        body = self._left.body
        body.addWidget(section("Conditions"))
        f = form()
        self.ambient = spin(-60, 200, 1, "°C", 1.0, "Température de l’air autour du boîtier")
        self.ambient.valueChanged.connect(lambda v: self._set_root(
            "ambient_temperature", self.ctx.units.temperature_from_display(v),
            "Air ambiant"))
        f.addRow("Air ambiant", self.ambient)
        self.pressure = spin(300, 1200, 0, "hPa", 1.0, "Pression atmosphérique (1013 hPa au "
                                                        "niveau de la mer)")
        self.pressure.valueChanged.connect(lambda v: self._set_root("ambient_pressure", v * 100,
                                                                    "Pression ambiante"))
        f.addRow("Pression", self.pressure)
        body.addLayout(f)

        self.tree = AirTree(self.ctx, kinds=("opening",))
        self.tree.setMinimumHeight(120)
        body.addWidget(self.tree, 1)
        self.add_button = QPushButton("+ Ouverture")
        self.add_button.setToolTip("Ajouter une entrée, une sortie ou un ventilateur")
        menu = QMenu(self.add_button)
        for tool in ("inlet", "outlet", "fan"):
            label, sc, tip = TOOLS[tool]
            a = menu.addAction(f"{label.removeprefix('+ ')}\t{sc}")
            a.setToolTip(tip)
            a.triggered.connect(lambda _=False, t=tool: self.use_tool(t))
        self.add_button.setMenu(menu)
        body.addWidget(self.add_button)

        self.estimate = EstimatePanel(self.ctx)
        body.addWidget(self.estimate)

    def _build_right(self) -> None:
        self._right = Card("Propriétés")
        self.stack = QStackedWidget()
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.setContentsMargins(0, 0, 0, 0)
        el.addWidget(muted("Sélectionnez une ouverture, ou cliquez sur une face de la boîte "
                           "dans la vue 3D pour y ajouter une entrée, une sortie ou un "
                           "ventilateur.\n\nRaccourcis : Entrée I · Sortie O · Ventilateur N · "
                           "Sélection V · Supprimer Suppr."))
        el.addStretch(1)
        self.opening_inspector = OpeningInspector(self)
        self.face_inspector = FaceInspector(self)
        self.walls_inspector = WallsInspector(self)
        for w in (empty, self.opening_inspector, self.face_inspector, self.walls_inspector):
            self.stack.addWidget(w)
        self.stack.currentChanged.connect(self._fit_stack)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.stack)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._right.body.addWidget(scroll, 1)

    def _build_toolbar(self) -> None:
        self._toolbar = QWidget()
        self._toolbar.setObjectName("transparent")
        lay = QHBoxLayout(self._toolbar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.tool_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self._toolbar)
        for key, (label, _sc, tip) in TOOLS.items():
            b = QToolButton()
            b.setText(label.removeprefix("+ ") if key == "select" else label)
            b.setToolTip(tip)
            b.setCheckable(True)
            set_prop(b, kind="segment")
            b.clicked.connect(lambda _=False, k=key: self.use_tool(k))
            group.addButton(b)
            lay.addWidget(b)
            self.tool_buttons[key] = b
        self.tool_buttons["select"].setChecked(True)
        lay.addSpacing(8)
        self.grille_button = QToolButton()
        self.grille_button.setText("Grille")
        self.grille_button.setToolTip("Ajouter ou retirer une grille sur l’ouverture "
                                      "sélectionnée")
        self.grille_button.clicked.connect(self.toggle_grille)
        lay.addWidget(self.grille_button)
        walls = QToolButton()
        walls.setText("Parois")
        walls.setToolTip("Parois du boîtier : matériau, épaisseur, échange avec l’extérieur")
        walls.clicked.connect(lambda: self.ctx.selection.set([WALLS_KEY]))
        lay.addWidget(walls)
        self.center_button = QToolButton()
        self.center_button.setText("Centrer")
        self.center_button.setToolTip("Centrer l’ouverture sélectionnée sur sa face")
        self.center_button.clicked.connect(
            lambda: self.current_opening() and self.center_opening(self.current_opening()))
        lay.addWidget(self.center_button)
        lay.addStretch(1)
        self.hint = QLabel("")
        set_prop(self.hint, role="muted")
        lay.addWidget(self.hint)

    def _build_actions(self) -> None:
        self._actions = []
        for key, (label, sc, _tip) in TOOLS.items():
            a = QAction(f"Air : {label.removeprefix('+ ')}", self._toolbar)
            a.setShortcut(QKeySequence(sc))
            a.triggered.connect(lambda _=False, k=key: self.use_tool(k))
            self._actions.append(a)
        a = QAction("Air : supprimer l’ouverture", self._toolbar)
        a.setShortcuts([QKeySequence(QKeySequence.StandardKey.Delete), QKeySequence("Backspace")])
        a.triggered.connect(lambda: self.current_opening()
                            and self.delete_opening(self.current_opening()))
        self._actions.append(a)

    def _fit_stack(self, index: int) -> None:
        """Size the stack to the page shown, not to the tallest page."""
        for i in range(self.stack.count()):
            pol = QSizePolicy.Policy.Preferred if i == index else QSizePolicy.Policy.Ignored
            self.stack.widget(i).setSizePolicy(pol, pol)
        self.stack.adjustSize()

    def left_panel(self) -> QWidget:
        return self._left

    def right_panel(self) -> QWidget:
        return self._right

    def viewport_toolbar(self) -> QWidget:
        return self._toolbar

    def actions(self) -> list[QAction]:
        return self._actions

    def scene(self):
        try:
            return air_items(self.ctx.project.get() or {}, self.ctx.project.last_valid_case)
        except ImportError:
            return []

    def alerts(self, project):
        return air_alerts(project.get() or {}, project.case)

    def deactivated(self) -> None:
        self.use_tool("select")

    # ------------------------------------------------------------------ state
    def current_opening(self) -> str | None:
        kind, name = parse_key(self.ctx.selection.current or "")
        return name if kind == "opening" else None

    def current_face(self) -> str | None:
        cur = self.ctx.selection.current or ""
        if cur.startswith(FACE_PREFIX):
            return cur[len(FACE_PREFIX):]
        name = self.current_opening()
        o = ops.find_opening(self.ctx.project.get() or {}, name) if name else None
        return o.get("face") if o else None

    def _alive(self) -> bool:
        """False once the window (and so this tab's widgets) has been destroyed: the tab
        is not a widget, so its signal connections outlive them."""
        return shiboken6.isValid(self._left) and shiboken6.isValid(self._right)

    def _on_selection_changed(self, _keys) -> None:
        if self._alive():
            self._on_selection()

    def _on_project_changed(self) -> None:
        if not self._alive():
            return
        self._refresh_conditions()
        self._refresh_inspector()
        self.scene_changed.emit()

    def _refresh_conditions(self) -> None:
        if not self._alive():
            return
        p = self.ctx.project
        set_value(self.ambient, self.ctx.units.temperature_to_display(
            float(p.get("ambient_temperature", default=25.0))))
        self.ambient.setSuffix(f" {self.ctx.units.temperature_symbol}")
        set_value(self.pressure, float(p.get("ambient_pressure", default=101325.0)) / 100)

    def _on_selection(self) -> None:
        cur = self.ctx.selection.current or ""
        if self.armed and cur.startswith(FACE_PREFIX):
            face = cur[len(FACE_PREFIX):]
            tool = self.armed
            self.use_tool("select")
            self.create_opening("outlet" if tool == "outlet" else "inlet", face, tool == "fan")
            return
        self._refresh_inspector()
        has_opening = self.current_opening() is not None
        self.grille_button.setEnabled(has_opening)
        self.center_button.setEnabled(has_opening)

    def _refresh_inspector(self) -> None:
        cur = self.ctx.selection.current or ""
        kind, name = parse_key(cur)
        data = self.ctx.project.get() or {}
        if kind == "opening" and ops.find_opening(data, name):
            self._right.set_title(f"Ouverture · {name}")
            self.opening_inspector.set_opening(name)
            self.stack.setCurrentWidget(self.opening_inspector)
        elif cur.startswith(FACE_PREFIX) and cur[len(FACE_PREFIX):] in FACE_LABELS:
            face = cur[len(FACE_PREFIX):]
            self._right.set_title(f"Face · {FACE_LABELS[face]}")
            self.face_inspector.set_face(face)
            self.stack.setCurrentWidget(self.face_inspector)
        elif cur in (WALLS_KEY, ENCLOSURE):
            self._right.set_title("Parois du boîtier")
            self.walls_inspector.refresh()
            self.stack.setCurrentWidget(self.walls_inspector)
        else:
            self._right.set_title("Propriétés")
            self.stack.setCurrentIndex(0)

    # ------------------------------------------------------------------ tools
    def use_tool(self, tool: str) -> None:
        """Arm a creation tool. With a face known (selected opening, or a face whose
        editor is not open), the opening is added there right away; else the next face
        clicked in 3D, or the point clicked in the face editor, receives it."""
        if tool != "select":
            face = self.current_face()
            if face is not None and not self.active_face_editor():
                self.armed = None
                self.create_opening("outlet" if tool == "outlet" else "inlet", face,
                                    tool == "fan")
                tool = "select"
        self.armed = None if tool == "select" else tool
        self.tool_buttons[tool].setChecked(True)
        if self.armed:
            self.hint.setText("Cliquez sur une face de la boîte" + (
                " ou dans l’éditeur de la face" if self.active_face_editor() else ""))
        else:
            self.hint.setText("")
        self.face_inspector.editor.set_armed(self.armed)

    def active_face_editor(self) -> bool:
        """True when the face inspector is open: tools then place where the user clicks."""
        return self.stack.currentWidget() is self.face_inspector

    # ------------------------------------------------------------------ edits
    def _set_root(self, key: str, value, label: str) -> None:
        self.ctx.project.edit(label, lambda d: d.__setitem__(key, value), merge_key=f"air:{key}")

    def create_opening(self, kind: str, face: str, fan: bool = False,
                       prefer: tuple[float, float] | None = None) -> str:
        created = {}

        def fn(d):
            created["o"] = ops.new_opening(d, kind, face, fan, prefer)

        label = "Ajouter un ventilateur" if fan else (
            "Ajouter une entrée" if kind == "inlet" else "Ajouter une sortie")
        self.ctx.project.edit(label, fn)
        name = created["o"]["name"]
        self.ctx.selection.set([opening_key(name)])
        if fan:
            self.opening_inspector.show_page("fan")
        self.ctx.show_message(f"« {name} » ajoutée sur la face {FACE_LABELS[face].lower()}.")
        return name

    def create_from_editor(self, face: str, uv: tuple[float, float]) -> None:
        tool = self.armed
        if not tool:
            return
        self.use_tool("select")
        self.create_opening("outlet" if tool == "outlet" else "inlet", face, tool == "fan",
                            prefer=uv)

    def edit_opening(self, name: str, label: str, fn, merge_key: str | None = None) -> None:
        def apply(d):
            o = ops.find_opening(d, name)
            if o is not None:
                fn(o)

        self.ctx.project.edit(label, apply, merge_key=merge_key)

    def drag_opening(self, name: str, center: tuple, size: tuple) -> None:
        def fn(o):
            o["center"] = [float(center[0]), float(center[1])]
            o["size"] = [float(size[0]), float(size[1])]

        self.edit_opening(name, "Déplacer l’ouverture", fn, merge_key=f"move:{name}")

    def center_opening(self, name: str) -> None:
        def fn(d):
            o = ops.find_opening(d, name)
            if o is None:
                return
            w, h = face_size(d, o["face"])
            c = clamp_center((w / 2, h / 2), (float(o["size"][0]), float(o["size"][1])), w, h)
            o["center"] = [c[0], c[1]]

        self.ctx.project.edit("Centrer l’ouverture", fn)

    def move_opening_to_face(self, name: str | None, face: str) -> None:
        if not name:
            return

        def fn(d):
            o = ops.find_opening(d, name)
            if o is not None and o.get("face") != face:
                ops.move_to_face(d, o, face)

        self.ctx.project.edit("Changer de face", fn)

    def rename_opening(self, old: str, new: str) -> bool:
        data = self.ctx.project.get() or {}
        if not NAME_RE.match(new):
            self.ctx.show_message("Nom invalide : une lettre puis lettres, chiffres ou _.")
            return False
        if new in used_names(data) - {old}:
            self.ctx.show_message(f"Le nom « {new} » est déjà pris.")
            return False
        # the selection first: the edit drops keys of objects that no longer exist
        self.ctx.selection.rename(opening_key(old), opening_key(new))
        self.ctx.project.edit("Renommer l’ouverture", lambda d: ops.rename_opening(d, old, new))
        return True

    def delete_opening(self, name: str) -> None:
        self.ctx.project.edit(f"Supprimer {name}", lambda d: ops.delete_opening(d, name))
        self.ctx.selection.set([k for k in self.ctx.selection.keys if k != opening_key(name)])

    def toggle_grille(self) -> None:
        name = self.current_opening()
        if not name:
            return
        on = not (ops.find_opening(self.ctx.project.get() or {}, name) or {}).get("grille")
        self.edit_opening(name, "Grille", lambda o: ops.set_grille(o, on))
        if on:
            self.opening_inspector.show_page("grille")

    def select_opening(self, name: str) -> None:
        self.ctx.selection.set([opening_key(name)])

    def select_face(self, face: str) -> None:
        self.ctx.selection.set([face_key(face)])

    def set_face_wall(self, face: str, choice: str, value: dict | None = None,
                      merge_key: str | None = None) -> None:
        def fn(d):
            if choice == "inherit":
                ops.set_face_override(d, face, None)
            elif choice == "adiabatic":
                ops.set_face_override(d, face, {"mode": "adiabatic"})
            else:
                base = value or {**ops.wall_for_face(d, face), "mode": "exchange"}
                ops.set_face_override(d, face, base)

        self.ctx.project.edit(f"Paroi {FACE_LABELS[face].lower()}", fn,
                              merge_key=merge_key and f"wall:{face}:{merge_key}")
