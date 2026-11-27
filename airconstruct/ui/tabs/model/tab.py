"""Step 1, Modèle: the enclosure and the solid parts.

Owned by: thread "Modèle" (see docs/ui-architecture.md).

Left: the model tree (groups, hidden and locked parts) and the parts library.
Middle: the shared 3D view with move / resize handles (snapped to a grid, Alt = free)
and this tab's toolbar. A part can also be dragged with the mouse: press on it and
move, it slides on the plane facing the camera (Shift: up and down), snapped to the
grid and stuck to the neighbour or wall it nearly touches (Alt: free). Right: the inspector of the selected parts (several at once)
and the live model checks (overlaps, parts out of the box).

Every change is one undoable project edit; the operations themselves are plain
functions in :mod:`.geometry`. Openings, fans on the walls and walls are set in the
Air tab; mesh and solver settings in the next steps.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolButton,
    QWidget,
)

from ...alerts import Alert
from ...selection import component_key, parse_key
from ...theme import COLORS, set_prop
from ...view import PASSIVE_COLORS, SceneItem, _heat_color, geometry_items
from ...viewport import Drag
from ...widgets.card import Card
from ..base import StepTab
from . import geometry as geo
from .checks import model_alerts
from .dialogs import ArrayDialog, MaterialDialog
from .handles import BoxHandles
from .inspector import ChecksView, Inspector
from .library import PRESETS
from .tree import ModelTree

SNAP_CHOICES = [(1.0, "1 mm"), (5.0, "5 mm"), (10.0, "10 mm"), (0.0, "libre")]
MODES = {"select": "Sélection", "move": "Déplacer", "resize": "Redimensionner"}


class ModelTab(StepTab):
    key = "model"
    title = "Modèle"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.mode = "select"
        self.isolated: list[str] | None = None
        self.snap_mm = float(ctx.settings.value("model/snap_mm", 5.0))
        self.handles = BoxHandles(ctx.viewport, self.handle_bounds)
        self._build_actions()
        self._build_left()
        self._build_right()
        self._build_toolbar()
        ctx.project.changed.connect(self._on_project_changed)
        ctx.project.replaced.connect(self._on_replaced)
        ctx.selection.changed.connect(lambda _k: self._on_selection_changed())
        ctx.viewport.redrawn.connect(self._update_handles)
        ctx.runner.running_changed.connect(lambda _r: self._update_actions())
        self._update_actions()

    # ------------------------------------------------------------------ helpers
    @property
    def parts(self) -> list[str]:
        data = self.ctx.project.get("components") or []
        known = {str(c.get("name")) for c in data}
        return [n for k in self.ctx.selection.keys for kind, n in [parse_key(k)]
                if kind == "component" and n in known]

    @property
    def snap(self) -> float | None:
        return self.snap_mm / 1000 if self.snap_mm > 0 else None

    def edit(self, label: str, fn: Callable[[dict], object], merge_key: str | None = None):
        """One undoable step; refused while a calculation runs. Returns fn's result."""
        if self.ctx.runner.running:
            self.ctx.show_message("Modification impossible pendant le calcul.")
            return None
        out = {}

        def run(d: dict) -> None:
            out["r"] = fn(d)

        self.ctx.project.edit(label, run, merge_key)
        return out.get("r")

    # ------------------------------------------------------------------ actions
    def _act(self, text: str, slot, keys=(), checkable: bool = False) -> QAction:
        a = QAction(text, self)
        if keys:
            a.setShortcuts([QKeySequence(k) for k in keys])
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        return a

    def _build_actions(self) -> None:
        self.mode_actions: dict[str, QAction] = {}
        group = QActionGroup(self)
        for mode, key in (("select", "V"), ("move", "G"), ("resize", "S")):
            a = self._act(f"{MODES[mode]} ({key})", lambda _=False, m=mode: self.set_mode(m),
                          [key], checkable=True)
            group.addAction(a)
            self.mode_actions[mode] = a
        self.mode_actions["select"].setChecked(True)
        self.rest_action = self._act("Poser sur ce qui est dessous (D)", self.drop, ["D"])
        self.duplicate_action = self._act("Dupliquer (Ctrl D)", self.duplicate, ["Ctrl+D"])
        self.array_action = self._act("Réseau linéaire…", self.array)
        self.group_action = self._act("Grouper (Ctrl G)", self.group, ["Ctrl+G"])
        self.ungroup_action = self._act("Dégrouper (Ctrl Maj G)", self.ungroup,
                                        ["Ctrl+Shift+G"])
        self.isolate_action = self._act("Isoler la sélection (H)", self.toggle_isolate, ["H"],
                                        checkable=True)
        self.hide_action = self._act("Masquer / afficher (Maj H)", self.toggle_hidden,
                                     ["Shift+H"])
        self.lock_action = self._act("Verrouiller / déverrouiller (Ctrl L)", self.toggle_locked,
                                     ["Ctrl+L"])
        self.delete_action = self._act("Supprimer (Suppr)", self.delete,
                                       [QKeySequence.StandardKey.Delete, "Backspace"])
        self.rename_action = self._act("Renommer (F2)", self.focus_name, ["F2"])
        self.add_action = self._act("Ajouter un bloc libre", lambda: self.add_preset("block"))
        self.align_menu = QMenu("Aligner / répartir")
        self.align_actions: dict[str, QAction] = {}
        for how, text in geo.ALIGN.items():
            if how == "xdist":
                self.align_menu.addSeparator()
            a = self.align_menu.addAction(text)
            a.triggered.connect(lambda _=False, h=how: self.align(h))
            self.align_actions[how] = a

    def actions(self) -> list[QAction]:
        return [*self.mode_actions.values(), self.rest_action, self.duplicate_action,
                self.group_action, self.ungroup_action, self.isolate_action, self.hide_action,
                self.lock_action, self.delete_action, self.rename_action]

    def _update_actions(self) -> None:
        """Enable what applies to the selection; nothing while another tab is open
        (the shortcuts would act on parts the user does not see)."""
        n = len(self.parts)
        free = self.active and not self.ctx.runner.running
        for a in self.mode_actions.values():
            a.setEnabled(self.active)
        self.isolate_action.setEnabled(self.active)
        for a in (self.rest_action, self.duplicate_action, self.array_action,
                  self.hide_action, self.lock_action, self.delete_action):
            a.setEnabled(free and n > 0)
        self.rename_action.setEnabled(free and n == 1)
        self.group_action.setEnabled(free and n > 1)
        data = self.ctx.project.get() or {}
        self.ungroup_action.setEnabled(free and any(geo.group_of(data, p) for p in self.parts))
        for how, a in self.align_actions.items():
            a.setEnabled(free and n >= (3 if how.endswith("dist") else 2))
        self.align_button.setEnabled(free and n >= 2)
        for b in [*self.library_buttons, self.add_button]:
            b.setEnabled(not self.ctx.runner.running)

    # ------------------------------------------------------------------ panels
    def _build_left(self) -> None:
        self._left = QSplitter(Qt.Orientation.Vertical)
        self._left.setProperty("cards", True)
        self._left.setChildrenCollapsible(False)
        self._left.setHandleWidth(10)
        tree_card = Card("Modèle", margins=(6, 6, 6, 6))
        self.tree = ModelTree(self.ctx)
        self.tree.context_menu_requested.connect(self._tree_menu)
        tree_card.body.addWidget(self.tree, 1)
        add = QPushButton("+ Ajouter une pièce")
        set_prop(add, kind="flat")
        add.setStyleSheet(f"color: {COLORS['accent']}; text-align: left;")
        add.clicked.connect(lambda: self.add_preset("block"))
        self.add_button = add
        tree_card.body.addWidget(add)
        lib = Card("Bibliothèque · cliquer pour ajouter")
        grid = QGridLayout()
        grid.setSpacing(6)
        self.library_buttons: list[QPushButton] = []
        for i, p in enumerate(PRESETS):
            b = QPushButton(p.label)
            b.setToolTip(f"{p.hint}\nPosée sur la pièce sélectionnée, sinon au fond de la boîte.")
            b.clicked.connect(lambda _=False, k=p.kind: self.add_preset(k))
            grid.addWidget(b, i // 2, i % 2)
            self.library_buttons.append(b)
        lib.body.addLayout(grid)
        self._left.addWidget(tree_card)
        self._left.addWidget(lib)
        self._left.setStretchFactor(0, 1)
        self._left.setStretchFactor(1, 0)

    def _build_right(self) -> None:
        self._right = QSplitter(Qt.Orientation.Vertical)
        self._right.setProperty("cards", True)
        self._right.setChildrenCollapsible(False)
        self._right.setHandleWidth(10)
        props = Card("Propriétés")
        self.inspector = Inspector(self.ctx)
        scroll = QScrollArea()
        scroll.setWidget(self.inspector)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        props.body.addWidget(scroll, 1)
        checks = Card("Contrôles du modèle")
        self.checks = ChecksView(self.ctx)
        cs = QScrollArea()
        cs.setWidget(self.checks)
        cs.setWidgetResizable(True)
        cs.setFrameShape(QFrame.Shape.NoFrame)
        checks.body.addWidget(cs, 1)
        self._right.addWidget(props)
        self._right.addWidget(checks)
        self._right.setStretchFactor(0, 1)
        self._right.setStretchFactor(1, 0)
        self._right.setSizes([620, 160])

        i = self.inspector
        i.rename_requested.connect(self.rename)
        i.material_requested.connect(self.set_material)
        i.new_material_requested.connect(self.new_material)
        i.edit_material_requested.connect(self.edit_material)
        i.power_requested.connect(lambda v: self._set_field("power", v, "Puissance"))
        i.tmax_requested.connect(lambda v: self._set_field("max_temperature", v,
                                                           "Température max"))
        i.rest_on_requested.connect(self.rest_on)
        i.position_requested.connect(self.set_position)
        i.size_requested.connect(self.set_size)
        i.enclosure_requested.connect(self.set_enclosure)
        i.duplicate_button.clicked.connect(self.duplicate)
        i.array_button.clicked.connect(self.array)
        i.hide_button.clicked.connect(self.toggle_hidden)
        i.lock_button.clicked.connect(self.toggle_locked)
        i.delete_button.clicked.connect(self.delete)

    def _tool(self, action: QAction, text: str) -> QToolButton:
        """A short-labelled button that follows an action (enabled, checked); the
        tooltip gives the full name and shortcut."""
        b = QToolButton()
        b.setText(text)
        b.setToolTip(action.text())
        b.setCheckable(action.isCheckable())

        def sync() -> None:
            b.setEnabled(action.isEnabled())
            b.setChecked(action.isChecked())

        action.changed.connect(sync)
        b.clicked.connect(lambda _=False: action.trigger())
        sync()
        return b

    def _build_toolbar(self) -> None:
        bar = QWidget()
        bar.setObjectName("transparent")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.mode_buttons = {}
        for mode, a in self.mode_actions.items():
            b = self._tool(a, MODES[mode])
            b.setProperty("kind", "segment")
            lay.addWidget(b)
            self.mode_buttons[mode] = b
        lay.addWidget(self._tool(self.rest_action, "Poser sur"))
        self.align_button = QToolButton()
        self.align_button.setText("Aligner ▾")
        self.align_button.setToolTip("Aligner / répartir la sélection")
        self.align_button.setMenu(self.align_menu)
        self.align_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        lay.addWidget(self.align_button)
        lay.addWidget(self._separator())
        for a, text in ((self.duplicate_action, "Dupliquer"), (self.array_action, "Réseau"),
                        (self.group_action, "Grouper"), (self.isolate_action, "Isoler"),
                        (self.delete_action, "Supprimer")):
            lay.addWidget(self._tool(a, text))
        lay.addWidget(self._separator())
        self.snap_button = QToolButton()
        self.snap_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.snap_button)
        for mm, text in SNAP_CHOICES:
            a = menu.addAction(text)
            a.triggered.connect(lambda _=False, v=mm: self.set_snap(v))
        self.snap_button.setMenu(menu)
        self.snap_button.setToolTip("Pas d’accroche des poignées (Alt pendant le glisser : "
                                    "libre)")
        lay.addWidget(self.snap_button)
        hint = QLabel("· Alt = libre")
        set_prop(hint, role="muted")
        lay.addWidget(hint)
        lay.addStretch(1)
        self._toolbar = bar
        self._update_snap_label()

    @staticmethod
    def _separator() -> QFrame:
        s = QFrame()
        s.setFixedSize(1, 22)
        s.setStyleSheet(f"background: {COLORS['input_border']};")
        return s

    def left_panel(self) -> QWidget:
        return self._left

    def right_panel(self) -> QWidget:
        return self._right

    def viewport_toolbar(self) -> QWidget:
        return self._toolbar

    # ------------------------------------------------------------------ scene
    def scene(self) -> list[SceneItem] | None:
        try:
            return scene_items(self.ctx.project.get() or {}, self.ctx.project.last_valid_case,
                               self.isolated)
        except ImportError:
            return []

    def alerts(self, project) -> list[Alert]:
        return model_alerts(project.get() or {}, project.validation_error)

    # ------------------------------------------------------------------ life cycle
    def activated(self) -> None:
        self._update_actions()
        self._update_handles()

    def deactivated(self) -> None:
        self.handles.show(None)
        self._update_actions()

    def _on_project_changed(self) -> None:
        if self.isolated is not None:
            names = set(geo.names(self.ctx.project.get() or {}))
            self.isolated = [n for n in self.isolated if n in names] or None
            self.isolate_action.setChecked(self.isolated is not None)
        self._update_actions()

    def _on_replaced(self) -> None:
        self.isolated = None
        self.isolate_action.setChecked(False)

    def _on_selection_changed(self) -> None:
        self._update_actions()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.mode_actions[mode].setChecked(True)
        self._update_handles()

    def set_snap(self, mm: float) -> None:
        self.snap_mm = mm
        self.ctx.settings.setValue("model/snap_mm", mm)
        self._update_snap_label()

    def _update_snap_label(self) -> None:
        label = dict(SNAP_CHOICES).get(self.snap_mm, f"{self.snap_mm:g} mm")
        self.snap_button.setText(f"Accroche {label} ▾")

    # ------------------------------------------------------------------ handles
    def handle_targets(self) -> list[str]:
        """Parts the handles act on (unlocked selected parts and their groups)."""
        data = self.ctx.project.get() or {}
        locked = set(geo.read_meta(data)["locked"])
        return [n for n in self.parts if n not in locked]

    def _update_handles(self) -> None:
        if not self.active or self.mode == "select":
            self.handles.show(None)
            return
        targets = self.handle_targets()
        data = self.ctx.project.get() or {}
        if self.mode == "move":
            targets = geo.with_groups(data, targets)
        bb = geo.bbox(data, targets) if targets else None
        self.handles.show(None if bb is None else
                          (bb[0][0], bb[1][0], bb[0][1], bb[1][1], bb[0][2], bb[1][2]))

    def handle_bounds(self, bounds: tuple, free: bool = False) -> None:
        """New bounds from the 3D handles (mouse released)."""
        targets = self.handle_targets()
        step = None if free else self.snap
        if not targets:
            return
        if self.mode == "resize" and len(targets) == 1:
            self.edit(f"Redimensionner {targets[0]}",
                                lambda d: geo.apply_bounds(d, targets[0], bounds, "resize", step))
        else:
            def move(d: dict) -> None:
                members = geo.with_groups(d, targets)
                lo = geo.bbox(d, members)[0]
                geo.move_by(d, targets, [bounds[2 * i] - lo[i] for i in range(3)], step)

            label = f"Déplacer {targets[0]}" if len(targets) == 1 else "Déplacer la sélection"
            self.edit(label, move)
        self._update_handles()  # put the handles back on the data

    # ------------------------------------------------------------------ mouse drag
    def drag(self, key: str, point: tuple) -> Drag | None:
        """Press on a part in the 3D view: drag it (with the other selected parts
        when it is selected, and their groups). Locked parts and the enclosure let
        the camera turn instead."""
        kind, name = parse_key(key)
        if kind != "component" or self.ctx.runner.running or self.ctx.project.locked:
            return None
        data = self.ctx.project.get() or {}
        locked = set(geo.read_meta(data)["locked"])
        if name in locked or geo.comp(data, name) is None:
            return None
        targets = self.handle_targets() if name in self.parts else [name]
        members = [m for m in geo.with_groups(data, targets) if m not in locked]
        self.handles.show(None)  # put back when the drag ends
        return Drag(keys=[component_key(m) for m in members],
                    constrain=lambda d, free: tuple(self.drag_delta(targets, d, free)),
                    finish=lambda d, free: self.drag_finish(name, targets, d, free),
                    cancel=self._update_handles)

    def drag_delta(self, targets: list[str], raw, free: bool = False) -> list[float]:
        """The move of a mouse drag: whole grid steps, then stuck to a neighbour or a
        wall within reach (:func:`geometry.magnet`); exactly the mouse with Alt."""
        if free:
            return [float(v) for v in raw]
        data = self.ctx.project.get() or {}
        d = [0.0 if abs(v) < 1e-7 else geo.snap(v, self.snap) for v in raw]
        return geo.magnet(data, targets, d, geo.magnet_tolerance(data))

    def drag_finish(self, name: str, targets: list[str], raw, free: bool = False) -> bool:
        """Mouse released: one undoable move. True if the parts moved."""
        delta = self.drag_delta(targets, raw, free)
        moved = []
        if any(abs(v) > geo.EPS for v in delta):
            label = f"Déplacer {targets[0]}" if len(targets) == 1 else "Déplacer la sélection"
            moved = self.edit(label, lambda d: geo.move(d, targets, delta)) or []
        if moved and name not in self.parts:
            self.ctx.selection.set([component_key(name)])
        self._update_handles()
        return bool(moved)

    # ------------------------------------------------------------------ operations
    def add_preset(self, kind: str) -> str | None:
        from .library import PRESET_BY_KIND

        preset = PRESET_BY_KIND[kind]
        on = self.parts[-1] if len(self.parts) == 1 else None
        name = self.edit(f"Ajouter {preset.label.lower()}",
                         lambda d: geo.add_part(d, preset.part(), kind, on))
        if name:
            self.ctx.selection.set([component_key(name)])
        return name

    def duplicate(self) -> list[str]:
        parts = self.parts
        if not parts:
            return []
        new = self.edit("Dupliquer", lambda d: geo.duplicate(d, parts)) or []
        if new:
            self.ctx.selection.set([component_key(n) for n in new])
        return new

    def array(self, values: tuple[int, int, float] | None = None) -> list[str]:
        parts = self.parts
        if not parts:
            return []
        if not values:
            data = self.ctx.project.get() or {}
            bb = geo.bbox(data, geo.with_groups(data, parts))
            pitch = {i: bb[1][i] - bb[0][i] + 0.002 for i in range(3)}
            dlg = ArrayDialog(self.ctx.units, pitch, self._right)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return []
            values = dlg.values()
        count, axis, pitch = values
        new = self.edit(f"Réseau de {count}", lambda d: geo.linear_array(d, parts, count, axis,
                                                                          pitch)) or []
        if new:
            self.ctx.selection.set([component_key(n) for n in parts + new])
        return new

    def delete(self) -> None:
        parts = self.parts
        if parts:
            label = f"Supprimer {parts[0]}" if len(parts) == 1 else f"Supprimer {len(parts)} pièces"
            self.edit(label, lambda d: geo.delete(d, parts))

    def rename(self, old: str, new: str) -> None:
        if geo.rename_error(self.ctx.project.get() or {}, old, new) or self.ctx.runner.running:
            return
        # selection first: the edit drops keys of parts that no longer exist
        self.ctx.selection.rename(component_key(old), component_key(new))
        self.edit(f"Renommer {old}", lambda d: geo.rename(d, old, new))

    def focus_name(self) -> None:
        if len(self.parts) == 1:
            self.inspector.name_edit.setFocus()
            self.inspector.name_edit.selectAll()

    def group(self) -> None:
        parts = self.parts
        if len(parts) > 1:
            self.edit("Grouper", lambda d: geo.group(d, parts))

    def ungroup(self) -> None:
        parts = self.parts
        if parts:
            self.edit("Dégrouper", lambda d: geo.ungroup(d, parts))

    def toggle_hidden(self) -> None:
        parts = self.parts
        if parts:
            hidden = set(geo.read_meta(self.ctx.project.get() or {})["hidden"])
            on = not all(p in hidden for p in parts)
            self.edit("Masquer" if on else "Afficher",
                      lambda d: geo.set_flag(d, "hidden", parts, on))
            self.scene_changed.emit()

    def toggle_locked(self) -> None:
        parts = self.parts
        if parts:
            locked = set(geo.read_meta(self.ctx.project.get() or {})["locked"])
            on = not all(p in locked for p in parts)
            self.edit("Verrouiller" if on else "Déverrouiller",
                      lambda d: geo.set_flag(d, "locked", parts, on))
            self._update_handles()

    def toggle_isolate(self) -> None:
        if self.isolated is None and self.parts:
            self.isolated = list(self.parts)
        else:
            self.isolated = None
        self.isolate_action.setChecked(self.isolated is not None)
        self.scene_changed.emit()

    def drop(self) -> None:
        self.rest_on(None)

    def rest_on(self, target) -> None:
        parts = self.parts
        if parts:
            label = ("Poser au fond" if target == "floor" else
                     f"Poser sur {target}" if target else "Poser")
            self.edit(label, lambda d: geo.rest_on(d, parts, target))

    def align(self, how: str) -> None:
        parts = self.parts
        if len(parts) > 1:
            self.edit(geo.ALIGN[how], lambda d: geo.align(d, parts, how))

    def set_position(self, axis: int, value: float) -> None:
        parts = self.parts
        if parts:
            self.edit(f"Position {'xyz'[axis]}",
                      lambda d: geo.set_position(d, parts, axis, value),
                      merge_key=f"pos:{axis}:{','.join(parts)}")

    def set_size(self, axis: int, value: float) -> None:
        parts = self.parts
        if parts:
            self.edit(f"Taille d{'xyz'[axis]}", lambda d: geo.set_size(d, parts, axis, value),
                      merge_key=f"size:{axis}:{','.join(parts)}")

    def set_enclosure(self, axis: int, value: float) -> None:
        def fn(d: dict) -> None:
            d.setdefault("enclosure", {})["xyz"[axis]] = value

        self.edit("Taille de l’enceinte", fn, merge_key=f"enclosure:{axis}")

    def _set_field(self, field: str, value, label: str) -> None:
        parts = self.parts

        def fn(d: dict) -> None:
            for n in parts:
                c = geo.comp(d, n)
                if c is None:
                    continue
                if value is None:
                    c.pop(field, None)
                else:
                    c[field] = value

        if parts:
            self.edit(label, fn, merge_key=f"{field}:{','.join(parts)}")

    def set_material(self, material: str) -> None:
        self._set_field("material", material, "Matériau")

    def new_material(self, values: dict | None = None) -> None:
        """Create a custom material (dialog, or ``values``) and give it to the selection."""
        data = self.ctx.project.get() or {}
        if values is None:
            taken = {str(m.get("name")) for m in data.get("materials") or []}
            dlg = MaterialDialog(taken, None, self._right)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            values = dlg.result_material
        parts = self.parts

        def fn(d: dict) -> None:
            d.setdefault("materials", []).append(dict(values))
            for n in parts:
                c = geo.comp(d, n)
                if c is not None:
                    c["material"] = values["name"]

        self.edit(f"Matériau {values['name']}", fn)

    def edit_material(self, name: str, values: dict | None = None) -> None:
        data = self.ctx.project.get() or {}
        current = next((m for m in data.get("materials") or [] if m.get("name") == name), None)
        if current is None:
            return
        if values is None:
            taken = {str(m.get("name")) for m in data.get("materials") or []}
            dlg = MaterialDialog(taken, current, self._right)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            values = dlg.result_material

        def fn(d: dict) -> None:
            mats = d.setdefault("materials", [])
            for i, m in enumerate(mats):
                if m.get("name") == name:
                    mats[i] = dict(values)
            for c in d.get("components") or []:
                if c.get("material") == name:
                    c["material"] = values["name"]

        self.edit(f"Matériau {values['name']}", fn)

    # ------------------------------------------------------------------ menus
    def _tree_menu(self, pos) -> None:
        if not self.parts:
            return
        m = QMenu(self.tree)
        for a in (self.rename_action, self.duplicate_action, self.array_action, None,
                  self.group_action, self.ungroup_action, None, self.rest_action,
                  self.hide_action, self.lock_action, None, self.delete_action):
            if a is None:
                m.addSeparator()
            else:
                m.addAction(a)
        m.exec(pos)


# ---------------------------------------------------------------------- scene
def scene_items(data: dict, case, isolated: list[str] | None = None) -> list[SceneItem]:
    """What the Modèle tab draws: the enclosure and parts as they are in the data
    (even while it is not a valid case, so a part dragged out of the box shows where
    it is), the openings of the last valid case, hidden parts left out, parts with a
    problem outlined in red."""
    import pyvista as pv

    E = geo.enclosure(data)
    items = []
    if all(v > 0 for v in E):
        items.append(SceneItem("enclosure", pv.Box((0, E[0], 0, E[1], 0, E[2])), "#555555",
                               style="wireframe", key="enclosure"))
    m = geo.read_meta(data)
    hidden = set(m["hidden"])
    bad = {a for a, _b, _d in geo.overlaps(data)} | {b for _a, b, _d in geo.overlaps(data)}
    bad |= {n for n, _a, _v in geo.outside(data)}
    comps = data.get("components") or []
    p_max = max((geo._f(c.get("power")) for c in comps), default=0.0)
    for c in comps:
        name = str(c.get("name"))
        if name in hidden or (isolated is not None and name not in isolated):
            continue
        lo, hi = geo.lo_hi(c)
        if any(hi[i] - lo[i] <= 0 for i in range(3)):
            continue
        mesh = pv.Box((lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]))
        edge = COLORS["error"] if name in bad else "#333333"
        kw = {"show_edges": True, "edge_color": edge, "line_width": 2 if name in bad else 1}
        power = geo._f(c.get("power"))
        if power > 0:
            items.append(SceneItem(name, mesh, _heat_color(power, p_max), kwargs=kw,
                                   key=component_key(name)))
        else:
            material = c.get("material")
            items.append(SceneItem(name, mesh, PASSIVE_COLORS.get(material, "#9aa5b1"),
                                   opacity=1.0 if material == "fr4" else 0.45, kwargs=kw,
                                   key=component_key(name)))
    if case is not None and isolated is None:
        items += [i for i in geometry_items(case) if (i.key or "").startswith("opening:")]
    return items
