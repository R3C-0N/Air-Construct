"""Step 3, Volume d'air: mesh fineness, local refinement, mesh checks and a preview
of the real mesh with a cut plane.

Owned by: thread "Volume d'air" (see docs/ui-architecture.md).

Left panel: the fineness table (cells and duration per level), a continuous cell
size slider in expert mode, and the local refinements with what each one costs.
Right panel: the mesh checks; a failed check has a "Corriger" button when the fix is
automatic and a "Voir la pièce" button that selects and frames the part. The 3D view
shows the cells of the real grid (the one blockMesh will build) cut by a plane.
The numbers come from :mod:`.meshinfo`, i.e. from ``mesh.plan_mesh``.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from superqt import QLabeledDoubleSlider

from ...alerts import VOLUME, Alert
from ...selection import component_key, parse_key
from ...theme import COLORS, set_prop
from ...view import SceneItem, geometry_items
from ...widgets.card import Card
from ..base import StepTab
from . import meshinfo as mi

AXES = "xyz"


def _clear(layout) -> None:
    """Remove and delete every widget of a layout (hidden at once, not at the next
    event loop turn, so old rows never show under new ones)."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


class VolumeTab(StepTab):
    key = "volume"
    title = "Volume d’air"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._dirty = True
        self._case = None  # case shown (last valid one)
        self._rows: list[mi.LevelRow] = []
        self._checks: list[mi.Check] = []
        self._ref_widgets: dict[str, dict] = {}
        self.show_mesh = True
        self.color_by_size = False
        self.cut_axis, self.cut_pos = 1, 0.0
        self._cut_set = False  # the user (or a "Voir") placed the cut

        self._build_left()
        self._build_right()
        self._build_toolbar()
        self._build_actions()

        p = ctx.project
        p.changed.connect(self._on_project_changed)
        p.replaced.connect(self._on_replaced)
        ctx.expert.changed.connect(lambda _on: self._update_expert())
        ctx.units.changed.connect(self._on_units_changed)
        ctx.selection.changed.connect(lambda _keys: self._update_toolbar_state())
        self._update_expert()
        self.refresh()

    # ================================================================ building
    def _build_left(self) -> None:
        self._left = Card("Volume d’air")
        lay = self._left.body

        self.table = QTableWidget(5, 3)
        self.table.setHorizontalHeaderLabels(["Finesse", "mailles", "durée"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setFixedHeight(32 * 6 + 4)
        for r in range(5):
            self.table.setRowHeight(r, 32)
        self.table.cellClicked.connect(lambda row, _col: self.set_level(row + 1))
        lay.addWidget(self.table)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        set_prop(self.summary, role="muted")
        lay.addWidget(self.summary)

        # expert: continuous cell size
        self.expert_box = QWidget()
        eb = QVBoxLayout(self.expert_box)
        eb.setContentsMargins(0, 4, 0, 0)
        eb.setSpacing(4)
        t = QLabel("Taille de maille")
        set_prop(t, role="section")
        eb.addWidget(t)
        self.size_slider = QLabeledDoubleSlider(Qt.Orientation.Horizontal)
        self.size_slider.setDecimals(1)
        self.size_slider.valueChanged.connect(self._on_size_slider)
        eb.addWidget(self.size_slider)
        row = QHBoxLayout()
        self.size_range_label = QLabel()
        set_prop(self.size_range_label, role="muted")
        row.addWidget(self.size_range_label, 1)
        self.levels_button = QPushButton("Revenir aux niveaux")
        set_prop(self.levels_button, kind="flat")
        self.levels_button.clicked.connect(lambda: self.set_level(self._level()))
        row.addWidget(self.levels_button)
        eb.addLayout(row)
        lay.addWidget(self.expert_box)

        sec = QLabel("Raffinement local")
        set_prop(sec, role="section")
        lay.addWidget(sec)
        self.ref_box = QWidget()
        self.ref_layout = QGridLayout(self.ref_box)
        self.ref_layout.setContentsMargins(0, 0, 0, 0)
        self.ref_layout.setHorizontalSpacing(6)
        self.ref_layout.setVerticalSpacing(4)
        lay.addWidget(self.ref_box)
        self.ref_empty = QLabel("Aucun. Raffinez autour d’une pièce chaude ou fine.")
        self.ref_empty.setWordWrap(True)
        set_prop(self.ref_empty, role="muted")
        lay.addWidget(self.ref_empty)
        self.add_ref_button = QPushButton("+ Raffiner autour d’une pièce")
        self.add_menu = QMenu(self.add_ref_button)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        self.add_ref_button.setMenu(self.add_menu)
        lay.addWidget(self.add_ref_button)
        lay.addStretch(1)

    def _build_right(self) -> None:
        self._right = Card("Contrôles")
        self.checks_pill = QLabel()
        set_prop(self.checks_pill, role="pill", tone="success")
        self._right.header_layout.addWidget(self.checks_pill)
        self.checks_box = QWidget()
        self.checks_layout = QVBoxLayout(self.checks_box)
        self.checks_layout.setContentsMargins(0, 0, 0, 0)
        self.checks_layout.setSpacing(10)
        self._right.body.addWidget(self.checks_box)
        self._right.body.addStretch(1)

    def _build_toolbar(self) -> None:
        self._toolbar = QWidget()
        tl = QHBoxLayout(self._toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        self.mesh_button = QToolButton()
        self.mesh_button.setText("Mailles")
        self.mesh_button.setToolTip("Afficher les mailles (C)")
        self.mesh_button.setCheckable(True)
        self.mesh_button.setChecked(True)
        self.mesh_button.setProperty("kind", "segment")
        self.mesh_button.toggled.connect(self.set_show_mesh)
        tl.addWidget(self.mesh_button)

        cut = QLabel("Coupe")
        set_prop(cut, role="muted")
        tl.addWidget(cut)
        self.axis_buttons: list[QToolButton] = []
        group = QButtonGroup(self._toolbar)
        for i, a in enumerate(AXES):
            b = QToolButton()
            b.setText(a)
            b.setToolTip(f"Plan de coupe perpendiculaire à {a}")
            b.setCheckable(True)
            b.setProperty("kind", "segment")
            b.clicked.connect(lambda _=False, i=i: self.set_cut(i, None))
            group.addButton(b)
            tl.addWidget(b)
            self.axis_buttons.append(b)
        self.cut_slider = QLabeledDoubleSlider(Qt.Orientation.Horizontal)
        self.cut_slider.setDecimals(1)
        self.cut_slider.setMinimumWidth(160)
        self.cut_slider.valueChanged.connect(self._on_cut_slider)
        tl.addWidget(self.cut_slider, 1)

        self.color_button = QToolButton()
        self.color_button.setText("Taille")
        self.color_button.setToolTip("Colorer les mailles par leur taille")
        self.color_button.setCheckable(True)
        self.color_button.setProperty("kind", "segment")
        self.color_button.toggled.connect(self.set_color_by_size)
        tl.addWidget(self.color_button)

        self.refine_sel_button = QToolButton()
        self.refine_sel_button.setText("Raffiner")
        self.refine_sel_button.setToolTip("Raffiner autour de la pièce sélectionnée")
        self.refine_sel_button.clicked.connect(self.refine_selection)
        tl.addWidget(self.refine_sel_button)
        self.frame_button = QToolButton()
        self.frame_button.setText("Cadrer")
        self.frame_button.setToolTip("Cadrer la sélection (F)")
        self.frame_button.clicked.connect(lambda: self.ctx.viewport.frame())
        tl.addWidget(self.frame_button)

        self.cut_info = QLabel()
        set_prop(self.cut_info, role="muted")
        self.cut_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.cut_info.setMinimumWidth(40)
        tl.addWidget(self.cut_info, 1)

    def _build_actions(self) -> None:
        self._actions = []
        a = QAction("Cadrer la sélection", self)
        a.setShortcut(QKeySequence("F"))
        a.triggered.connect(lambda: self.ctx.viewport.frame())
        self._actions.append(a)
        a = QAction("Afficher les mailles", self)
        a.setShortcut(QKeySequence("C"))
        a.triggered.connect(self.mesh_button.toggle)
        self._actions.append(a)

    # ================================================================ StepTab
    def left_panel(self) -> QWidget:
        return self._left

    def right_panel(self) -> QWidget:
        return self._right

    def viewport_toolbar(self) -> QWidget:
        return self._toolbar

    def actions(self) -> list[QAction]:
        return self._actions

    def activated(self) -> None:
        if self._dirty:
            self.refresh()

    def alerts(self, project) -> Iterable[Alert]:
        case = project.case
        if case is None:
            return []
        return [Alert(c.level, c.title, VOLUME, c.target) for c in mi.checks(case) if not c.ok]

    def scene(self) -> list[SceneItem] | None:
        case = self._case
        if case is None:
            return None
        items = geometry_items(case)
        if not self.show_mesh:
            return items
        for it in items:  # see-through parts so the cut stays visible
            if it.key and it.key.startswith("component:"):
                it.opacity = min(it.opacity, 0.3)
        grid = mi.cut_grid(case, self.cut_axis, self.cut_pos, mi.plan(case))
        kw = {"show_edges": True, "edge_color": COLORS["muted"], "line_width": 1,
              "lighting": False}
        if self.color_by_size:
            kw.update(cmap="viridis", scalar_bar_args={
                "title": "Taille de maille (mm)", "title_font_size": 14,
                "label_font_size": 12, "n_labels": 2, "fmt": "%.1f",
                "color": COLORS["text"], "width": 0.35, "position_x": 0.6,
                "position_y": 0.04})
            items.append(SceneItem("mesh_cut", grid, scalars="size_mm", kwargs=kw))
        else:
            kw.update(cmap=[COLORS["accent_soft"], COLORS["heat"]], clim=(0, 1),
                      show_scalar_bar=False)
            items.append(SceneItem("mesh_cut", grid, scalars="solid", kwargs=kw))
        return items

    # ================================================================ refresh
    def _on_project_changed(self) -> None:
        self._dirty = True
        if self.active:
            self.refresh()

    def _on_replaced(self) -> None:
        self._cut_set = False
        self._on_project_changed()

    def _on_units_changed(self) -> None:
        self._dirty = True
        self.refresh()

    def _estimate(self, case) -> float:
        return float(self.ctx.services.estimate_runtime(case))

    def refresh(self) -> None:
        """Recompute the table, refinements, checks and the cut from the project."""
        self._dirty = False
        project = self.ctx.project
        case = project.case or project.last_valid_case
        self._case = case
        valid = project.case is not None
        for w in (self.table, self.expert_box, self.add_ref_button, self.ref_box):
            w.setEnabled(valid)
        if case is None:
            self.summary.setText("Le modèle n’est pas valide : corrigez-le d’abord.")
            return
        if not self._cut_set:
            self.cut_axis, self.cut_pos = mi.default_cut(case)
        self.cut_pos = min(max(self.cut_pos, 0.0), case.enclosure.as_tuple()[self.cut_axis])
        self._refresh_table(case)
        self._refresh_expert(case)
        self._refresh_refinements(case)
        self._refresh_checks(case)
        self._refresh_cut_controls(case)
        self._update_toolbar_state()
        self.scene_changed.emit()

    def _level(self) -> int:
        return int(self.ctx.project.get("precision", "level", default=3) or 3)

    def _refresh_table(self, case) -> None:
        self._rows = mi.level_rows(case, self._estimate)
        free = bool(case.precision.cell_size)
        self.table.blockSignals(True)
        for r, row in enumerate(self._rows):
            for c, text in enumerate((row.name, mi.fmt_cells(row.cells),
                                      mi.fmt_duration(row.seconds))):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(f"maille de {self.ctx.units.fmt_length(row.cell_size)}")
                self.table.setItem(r, c, item)
        self.table.clearSelection()
        if not free:
            self.table.selectRow(case.precision.level - 1)
        self.table.blockSignals(False)
        p = mi.plan(case)
        size = self.ctx.units.fmt_length(p.cell_size)
        mode = "taille libre" if free else mi.LEVEL_NAMES[case.precision.level]
        self.summary.setText(f"{mode} : {mi.fmt_cells(p.n_cells)} mailles · "
                             f"{mi.fmt_duration(self._estimate(case))} · maille {size}")

    def _refresh_expert(self, case) -> None:
        u = self.ctx.units
        lo, hi = mi.cell_size_range(case)
        size = mi.bulk_cell_size(case)
        self.size_slider.blockSignals(True)
        self.size_slider.setRange(u.length_to_display(lo), u.length_to_display(hi))
        self.size_slider.setValue(u.length_to_display(min(max(size, lo), hi)))
        self.size_slider.blockSignals(False)
        self.size_range_label.setText(
            f"en {u.length_symbol}, de {u.fmt_length(lo)} (fin) à {u.fmt_length(hi)}")
        self.levels_button.setVisible(bool(case.precision.cell_size))

    def _update_expert(self) -> None:
        self.expert_box.setVisible(self.ctx.expert.on)
        for w in self._ref_widgets.values():
            w["margin"].setVisible(self.ctx.expert.on)

    # ---- refinements -----------------------------------------------------------
    def _refresh_refinements(self, case) -> None:
        targets = [r.target for r in case.refinements]
        if targets != list(self._ref_widgets):
            self._rebuild_ref_rows(targets)
        u = self.ctx.units
        for r in case.refinements:
            w = self._ref_widgets[r.target]
            w["factor"].blockSignals(True)
            w["factor"].setValue(r.factor)
            w["factor"].setEnabled(r.cell_size is None)
            w["factor"].setToolTip("" if r.cell_size is None else
                                   f"taille imposée : {u.fmt_length(r.cell_size)}")
            w["factor"].blockSignals(False)
            margin = r.margin if r.margin is not None else 3.0 * mi.plan(case).cell_size
            w["margin"].blockSignals(True)
            w["margin"].setValue(round(u.length_to_display(margin), 1))
            w["margin"].setSuffix(f" {u.length_symbol}")
            w["margin"].blockSignals(False)
            w["cost"].setText(mi.fmt_cost(mi.refinement_cost(case, r.target, self._estimate)))
        self.ref_empty.setVisible(not targets)
        self._update_expert()

    def _rebuild_ref_rows(self, targets: list[str]) -> None:
        _clear(self.ref_layout)
        self._ref_widgets = {}
        for row, name in enumerate(targets):
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {COLORS['heat']};")
            label = QPushButton(name)
            set_prop(label, kind="flat")
            label.setToolTip("Voir la pièce")
            label.clicked.connect(lambda _=False, n=name: self.show_part(component_key(n)))
            cost = QLabel()
            set_prop(cost, role="muted")
            factor = QSpinBox()
            factor.setRange(1, 8)
            factor.setPrefix("× ")
            factor.setMinimumWidth(56)
            factor.setToolTip("Mailles divisées par ce facteur autour de la pièce")
            factor.valueChanged.connect(lambda v, n=name: self.set_refinement(n, factor=v))
            margin = QDoubleSpinBox()
            margin.setDecimals(1)
            margin.setRange(0.0, 10_000.0)
            margin.setMinimumWidth(84)
            margin.setToolTip("Distance sur laquelle les mailles reviennent à la taille normale")
            margin.valueChanged.connect(
                lambda v, n=name: self.set_refinement(n, margin=self.ctx.units.length_from_display(v)))
            remove = QToolButton()
            remove.setText("✕")
            remove.setToolTip(f"Retirer le raffinement de {name}")
            remove.clicked.connect(lambda _=False, n=name: self.remove_refinement(n))
            r = 2 * row
            self.ref_layout.addWidget(dot, r, 0)
            self.ref_layout.addWidget(label, r, 1)
            self.ref_layout.addWidget(factor, r, 2)
            self.ref_layout.addWidget(margin, r, 3)
            self.ref_layout.addWidget(remove, r, 4)
            self.ref_layout.addWidget(cost, r + 1, 1, 1, 4)
            self._ref_widgets[name] = {"factor": factor, "margin": margin, "cost": cost,
                                       "label": label, "remove": remove}
        self.ref_layout.setColumnStretch(1, 1)

    def _fill_add_menu(self) -> None:
        self.add_menu.clear()
        case = self._case
        if case is None:
            return
        done = {r.target for r in case.refinements}
        sel = [parse_key(k)[1] for k in self.ctx.selection.keys
               if parse_key(k)[0] == "component"]
        names = [c.name for c in case.components if c.name not in done]
        names.sort(key=lambda n: (n not in sel, -next(c.power for c in case.components
                                                       if c.name == n)))
        for n in names:
            cost = mi.refinement_cost(
                mi.with_changes(case, lambda d, n=n: d.setdefault("refinements", []).append(
                    mi.default_refinement(n))), n, self._estimate)
            a = self.add_menu.addAction(f"{n}   ({mi.fmt_cost(cost)})")
            a.triggered.connect(lambda _=False, n=n: self.add_refinement(n))
        if not names:
            a = self.add_menu.addAction("Toutes les pièces sont déjà raffinées")
            a.setEnabled(False)

    # ---- checks -----------------------------------------------------------------
    def _refresh_checks(self, case) -> None:
        self._checks = mi.checks(case, self._estimate)
        _clear(self.checks_layout)
        self.check_rows: list[dict] = []
        bad = [c for c in self._checks if not c.ok]
        n = len(bad)
        self.checks_pill.setText("aucune alerte" if not n else
                                 f"{n} alerte" + ("s" if n > 1 else ""))
        tone = "success" if not n else (
            "error" if any(c.level == "error" for c in bad) else "warning")
        set_prop(self.checks_pill, role="pill", tone=tone)
        for c in self._checks:
            self.checks_layout.addWidget(self._check_row(c))

    def _check_row(self, c: mi.Check) -> QWidget:
        w = QFrame()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        icon = QLabel("✓" if c.ok else ("✕" if c.level == "error" else "!"))
        color = COLORS["success"] if c.ok else (
            COLORS["error_text"] if c.level == "error" else COLORS["warning_text"])
        icon.setStyleSheet(f"color: {color}; font-weight: 700; font-size: 14px;")
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        icon.setFixedWidth(16)
        lay.addWidget(icon)
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(f"<b>{c.title}</b>")
        title.setWordWrap(True)
        col.addWidget(title)
        if c.detail:
            d = QLabel(c.detail)
            d.setWordWrap(True)
            set_prop(d, role="muted")
            col.addWidget(d)
        row = {"check": c, "fix": None, "show": None}
        if not c.ok and (c.fix or c.target):
            buttons = QHBoxLayout()
            buttons.setSpacing(6)
            if c.fix:
                fix = QPushButton(c.fix)
                set_prop(fix, kind="primary")
                fix.setToolTip(f"Corriger automatiquement ({c.fix_cost})" if c.fix_cost
                               else "Corriger automatiquement")
                fix.clicked.connect(lambda _=False, fid=c.fix_id, t=c.fix: self.apply_fix(fid, t))
                buttons.addWidget(fix)
                row["fix"] = fix
            if c.target:
                show = QPushButton("Voir la pièce" if c.target.startswith("component:")
                                   else "Voir")
                show.clicked.connect(lambda _=False, k=c.target: self.show_part(k))
                buttons.addWidget(show)
                row["show"] = show
            buttons.addStretch(1)
            col.addLayout(buttons)
            if c.fix_cost:
                cost = QLabel(c.fix_cost)
                set_prop(cost, role="muted")
                col.addWidget(cost)
        lay.addLayout(col, 1)
        self.check_rows.append(row)
        return w

    # ---- cut --------------------------------------------------------------------
    def _refresh_cut_controls(self, case) -> None:
        u = self.ctx.units
        E = case.enclosure.as_tuple()
        for i, b in enumerate(self.axis_buttons):
            b.setChecked(i == self.cut_axis)
        self.cut_slider.blockSignals(True)
        self.cut_slider.setRange(0.0, u.length_to_display(E[self.cut_axis]))
        self.cut_slider.setValue(u.length_to_display(self.cut_pos))
        self.cut_slider.blockSignals(False)
        self._update_cut_info()

    def _update_cut_info(self) -> None:
        case = self._case
        if case is None:
            return
        text = f"Coupe {AXES[self.cut_axis]} = {self.ctx.units.fmt_length(self.cut_pos)}"
        sel = [parse_key(k)[1] for k in self.ctx.selection.keys
               if parse_key(k)[0] == "component"]
        if len(sel) == 1 and any(c.name == sel[0] for c in case.components):
            n, _ = mi.cells_across(case, sel[0])
            text += f" · {sel[0]} : {n} maille" + ("s" if n > 1 else "")
        self.cut_info.setText(text)

    def _update_toolbar_state(self) -> None:
        sel = [k for k in self.ctx.selection.keys if parse_key(k)[0] == "component"]
        done = {r.target for r in self._case.refinements} if self._case else set()
        self.refine_sel_button.setEnabled(
            self.ctx.project.case is not None
            and any(parse_key(k)[1] not in done for k in sel))
        self.frame_button.setEnabled(bool(self.ctx.selection.keys))
        self._update_cut_info()

    def _on_cut_slider(self, value: float) -> None:
        self.set_cut(None, self.ctx.units.length_from_display(value))

    # ================================================================ actions
    def set_level(self, level: int) -> None:
        def f(d):
            d["precision"] = {"level": int(level), "cell_size": None}
        self.ctx.project.edit(f"Finesse : {mi.LEVEL_NAMES[int(level)]}", f)

    def set_cell_size(self, size_m: float) -> None:
        def f(d):
            prec = dict(d.get("precision") or {})
            prec["cell_size"] = float(size_m)
            prec.setdefault("level", 3)
            d["precision"] = prec
        self.ctx.project.edit("Taille de maille", f, merge_key="volume:cell_size")

    def _on_size_slider(self, value: float) -> None:
        self.set_cell_size(self.ctx.units.length_from_display(value))

    def add_refinement(self, name: str) -> None:
        def f(d):
            refs = [r for r in d.get("refinements") or [] if r.get("target") != name]
            d["refinements"] = refs + [mi.default_refinement(name)]
        self.ctx.project.edit(f"Raffiner {name}", f)

    def refine_selection(self) -> None:
        done = {r.target for r in self._case.refinements} if self._case else set()
        for k in self.ctx.selection.keys:
            kind, name = parse_key(k)
            if kind == "component" and name not in done:
                self.add_refinement(name)

    def set_refinement(self, name: str, **values) -> None:
        def f(d):
            for r in d.get("refinements") or []:
                if r.get("target") == name:
                    r.update(values)
        self.ctx.project.edit(f"Raffinement {name}", f, merge_key=f"volume:refine:{name}")

    def remove_refinement(self, name: str) -> None:
        def f(d):
            d["refinements"] = [r for r in d.get("refinements") or [] if r.get("target") != name]
        self.ctx.project.edit(f"Retirer le raffinement de {name}", f)

    def apply_fix(self, fix_id: str, label: str = "Corriger") -> None:
        case = self.ctx.project.case
        if case is None:
            return
        self.ctx.project.edit(label, lambda d: mi.apply_fix(d, case, fix_id))
        self.ctx.show_message(f"{label} : fait")

    def show_part(self, key: str) -> None:
        """Select the object, put the cut through it and frame it."""
        self.ctx.selection.set([key])
        kind, name = parse_key(key)
        if kind == "component" and self._case is not None and any(
                c.name == name for c in self._case.components):
            axis, pos = mi.cut_through(self._case, name, self.cut_axis)
            self.set_cut(axis, pos)
        self.ctx.viewport.frame([key])

    def set_cut(self, axis: int | None, position: float | None) -> None:
        case = self._case
        if case is None:
            return
        E = case.enclosure.as_tuple()
        if axis is not None and axis != self.cut_axis:
            self.cut_axis = axis
            if position is None:
                position = E[axis] / 2
        if position is not None:
            self.cut_pos = min(max(position, 0.0), E[self.cut_axis])
        self._cut_set = True
        self._refresh_cut_controls(case)
        self.scene_changed.emit()

    def set_show_mesh(self, on: bool) -> None:
        self.show_mesh = bool(on)
        if self.mesh_button.isChecked() != self.show_mesh:
            self.mesh_button.setChecked(self.show_mesh)
        self.scene_changed.emit()

    def set_color_by_size(self, on: bool) -> None:
        self.color_by_size = bool(on)
        if self.color_button.isChecked() != self.color_by_size:
            self.color_button.setChecked(self.color_by_size)
        self.scene_changed.emit()
