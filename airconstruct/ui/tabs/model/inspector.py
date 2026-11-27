"""Properties of the selected parts (right panel of the Modèle tab).

Owned by: thread "Modèle" (see docs/ui-architecture.md).

One part: name, material (built-in or custom, with k / ρ / cp), power, maximum
allowed temperature, what it rests on, position and size. Several parts: the same
fields, empty ("mixte") where the parts differ; a value typed there is given to
every selected part, and the position moves the selection as a block. The
enclosure: its size.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ....materials import SOLIDS
from ...selection import ENCLOSURE, parse_key
from ...theme import COLORS, set_prop
from . import geometry as geo
from .fields import NumberField
from .library import PRESET_BY_KIND, material_label, material_props

CUSTOM_MATERIAL = "__custom__"
EDIT_MATERIAL = "__edit__"
FLOAT_SUPPORT = "__free__"


def _common(values: list):
    """The value shared by all, and whether they differ."""
    if not values:
        return None, False
    first = values[0]
    same = all(v == first for v in values)
    return (first if same else None), not same


class Inspector(QWidget):
    """Emits requests; the tab turns them into project edits (one undo step each)."""

    rename_requested = Signal(str, str)
    material_requested = Signal(str)  # material name
    new_material_requested = Signal()
    edit_material_requested = Signal(str)
    power_requested = Signal(float)
    tmax_requested = Signal(object)  # float or None
    rest_on_requested = Signal(object)  # "floor", part name, or None
    position_requested = Signal(int, float)  # axis, metres (selection's min corner)
    size_requested = Signal(int, float)  # axis, metres
    enclosure_requested = Signal(int, float)

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.parts: list[str] = []
        self._filling = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.title = QLabel("")
        set_prop(self.title, role="title")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.empty = QLabel("Sélectionnez une pièce dans l’arbre ou dans la vue 3D, "
                            "ou ajoutez-en une depuis la bibliothèque.")
        self.empty.setWordWrap(True)
        set_prop(self.empty, role="muted")
        lay.addWidget(self.empty)

        # ---- part fields
        self.part_box = QWidget()
        pl = QGridLayout(self.part_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setHorizontalSpacing(8)
        pl.setVerticalSpacing(6)
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._rename)
        self.name_label = QLabel("Nom")
        self.name_error = QLabel("")
        self.name_error.setWordWrap(True)
        self.name_error.setStyleSheet(f"color: {COLORS['error_text']}; font-size: 12px;")
        self.material = QComboBox()
        self.material.activated.connect(self._material_chosen)
        self.material_info = QLabel("")
        set_prop(self.material_info, role="muted")
        self.power = NumberField("W", minimum=0)
        self.power.committed.connect(lambda v: v is not None and self.power_requested.emit(v))
        self.tmax = NumberField(ctx.units.temperature_symbol, allow_empty=True)
        self.tmax.committed.connect(self._tmax)
        self.tmax.setToolTip("Critère vérifié dans les résultats. Vide : pas de limite.")
        self.kind_note = QLabel("")
        self.kind_note.setWordWrap(True)
        set_prop(self.kind_note, role="muted")
        r = 0
        for label, w in (("Nom", self.name_edit), ("Matériau", self.material)):
            lab = self.name_label if label == "Nom" else QLabel(label)
            pl.addWidget(lab, r, 0)
            pl.addWidget(w, r, 1)
            r += 1
            if label == "Nom":
                pl.addWidget(self.name_error, r, 0, 1, 2)
                r += 1
        pl.addWidget(self.material_info, r, 1)
        r += 1
        pl.addWidget(QLabel("Puissance"), r, 0)
        pl.addWidget(self.power, r, 1)
        r += 1
        pl.addWidget(QLabel("T max admissible"), r, 0)
        pl.addWidget(self.tmax, r, 1)
        r += 1
        pl.addWidget(self.kind_note, r, 0, 1, 2)
        pl.setColumnStretch(1, 1)
        lay.addWidget(self.part_box)

        # ---- placement
        self.place_box = QWidget()
        gl = QGridLayout(self.place_box)
        gl.setContentsMargins(0, 4, 0, 0)
        gl.setHorizontalSpacing(8)
        gl.setVerticalSpacing(6)
        sec = QLabel("PLACEMENT")
        set_prop(sec, role="section")
        gl.addWidget(sec, 0, 0, 1, 4)
        self.rest_label = QLabel("Poser sur")
        self.rest_on = QComboBox()
        self.rest_on.activated.connect(self._rest_chosen)
        gl.addWidget(self.rest_label, 1, 0)
        gl.addWidget(self.rest_on, 1, 1, 1, 3)
        self.pos: list[NumberField] = []
        self.size: list[NumberField] = []
        for i, a in enumerate("xyz"):
            p = NumberField(ctx.units.length_symbol)
            s = NumberField(ctx.units.length_symbol, minimum=0)
            p.committed.connect(lambda v, i=i: self._pos(i, v))
            s.committed.connect(lambda v, i=i: self._size(i, v))
            gl.addWidget(QLabel(a), 2 + i, 0)
            gl.addWidget(p, 2 + i, 1)
            gl.addWidget(QLabel("d" + a), 2 + i, 2)
            gl.addWidget(s, 2 + i, 3)
            self.pos.append(p)
            self.size.append(s)
        gl.setColumnStretch(1, 1)
        gl.setColumnStretch(3, 1)
        lay.addWidget(self.place_box)

        # ---- action buttons (connected by the tab)
        self.buttons_box = QWidget()
        bl = QGridLayout(self.buttons_box)
        bl.setContentsMargins(0, 4, 0, 0)
        bl.setSpacing(6)
        self.duplicate_button = QPushButton("Dupliquer")
        self.array_button = QPushButton("Réseau…")
        self.hide_button = QPushButton("Masquer")
        self.lock_button = QPushButton("Verrouiller")
        self.delete_button = QPushButton("Supprimer")
        set_prop(self.delete_button, kind="danger")
        for i, b in enumerate((self.duplicate_button, self.array_button, self.hide_button,
                               self.lock_button, self.delete_button)):
            bl.addWidget(b, i // 2, i % 2)
        lay.addWidget(self.buttons_box)
        lay.addStretch(1)

        ctx.selection.changed.connect(lambda _k: self.refresh())
        ctx.project.changed.connect(self.refresh)
        ctx.units.changed.connect(self._units_changed)
        self.refresh()

    # ------------------------------------------------------------------ fill
    def _units_changed(self) -> None:
        for f in self.pos + self.size:
            f.set_unit(self.ctx.units.length_symbol)
        self.tmax.set_unit(self.ctx.units.temperature_symbol)
        self.refresh()

    def refresh(self) -> None:
        data = self.ctx.project.get() or {}
        keys = self.ctx.selection.keys
        self.parts = [n for k in keys for kind, n in [parse_key(k)]
                      if kind == "component" and n and geo.comp(data, n) is not None]
        self._filling = True
        try:
            if self.parts:
                self._fill_parts(data)
            elif ENCLOSURE in keys:
                self._fill_enclosure(data)
            else:
                self.title.setText("Propriétés")
                for w in (self.part_box, self.place_box, self.buttons_box):
                    w.setVisible(False)
                self.empty.setVisible(True)
        finally:
            self._filling = False

    def _fill_enclosure(self, data: dict) -> None:
        u = self.ctx.units
        self.title.setText("Boîte · enceinte")
        self.empty.setVisible(False)
        self.part_box.setVisible(False)
        self.buttons_box.setVisible(False)
        self.place_box.setVisible(True)
        self.rest_label.setVisible(False)
        self.rest_on.setVisible(False)
        E = geo.enclosure(data)
        for i in range(3):
            self.pos[i].setEnabled(False)
            self.pos[i].set_value(0.0)
            self.size[i].setEnabled(True)
            self.size[i].set_value(u.length_to_display(E[i]))

    def _fill_parts(self, data: dict) -> None:
        u = self.ctx.units
        m = geo.read_meta(data)
        comps = [geo.comp(data, n) for n in self.parts]
        one = len(comps) == 1
        self.empty.setVisible(False)
        for w in (self.part_box, self.place_box, self.buttons_box):
            w.setVisible(True)
        self.rest_label.setVisible(True)
        self.rest_on.setVisible(True)
        kind = m["kinds"].get(self.parts[0]) if one else None
        label = PRESET_BY_KIND[kind].label if kind in PRESET_BY_KIND else None
        self.title.setText(f"{self.parts[0]} · {label}" if one and label else
                           self.parts[0] if one else f"{len(comps)} pièces")
        self.kind_note.setText(
            "Ventilateur interne : bloc solide pour l’instant, son débit n’est pas encore "
            "simulé. Un ventilateur sur une paroi se règle dans l’onglet Air."
            if kind == "fan" else "")
        self.kind_note.setVisible(kind == "fan")
        # name
        self.name_label.setVisible(one)
        self.name_edit.setVisible(one)
        if one and not self.name_edit.hasFocus():
            self.name_edit.setText(self.parts[0])
            self.name_error.setText("")
        self.name_error.setVisible(one and bool(self.name_error.text()))
        # material
        mat, mixed = _common([c.get("material") for c in comps])
        self.material.clear()
        customs = [str(x.get("name")) for x in data.get("materials") or []]
        if mixed:
            self.material.addItem("mixte", None)
        for name in sorted(SOLIDS, key=material_label):
            self.material.addItem(material_label(name), name)
        if customs:
            self.material.insertSeparator(self.material.count())
            for name in customs:
                self.material.addItem(f"{name} (perso)", name)
        if mat is not None and self.material.findData(mat) < 0:
            self.material.addItem(f"{mat} (inconnu)", mat)
        self.material.insertSeparator(self.material.count())
        self.material.addItem("Matériau perso…", CUSTOM_MATERIAL)
        if mat in customs:
            self.material.addItem(f"Modifier {mat}…", EDIT_MATERIAL)
        self.material.setCurrentIndex(0 if mixed else max(0, self.material.findData(mat)))
        props = material_props(data, mat) if mat else None
        self.material_info.setText(
            f"k {props[0]:g} · ρ {props[1]:g} · cp {props[2]:g}" if props else "")
        # power, max temperature
        powers = [geo._f(c.get("power")) for c in comps]
        p, mixed = _common(powers)
        self.power.set_value(p, mixed)
        tmaxes = [c.get("max_temperature") for c in comps]
        t, mixed = _common(tmaxes)
        self.tmax.set_value(None if t is None else u.temperature_to_display(float(t)), mixed)
        # rest on
        self.rest_on.clear()
        supports = [geo.support_of(data, n) for n in self.parts]
        sup, mixed = _common(supports)
        if mixed:
            self.rest_on.addItem("mixte", None)
        self.rest_on.addItem("rien (libre)", FLOAT_SUPPORT)
        self.rest_on.addItem("le fond de l’enceinte", "floor")
        group = set(geo.with_groups(data, self.parts))
        for c in data.get("components") or []:
            n = str(c.get("name"))
            if n not in group:
                self.rest_on.addItem(f"{n} · face du dessus", n)
        idx = 0 if mixed else self.rest_on.findData(sup if sup else FLOAT_SUPPORT)
        self.rest_on.setCurrentIndex(max(0, idx))
        # position (selection's min corner), sizes
        bb = geo.bbox(data, self.parts)
        for i in range(3):
            self.pos[i].setEnabled(True)
            self.pos[i].set_value(u.length_to_display(bb[0][i]))
            sizes = [round(geo.lo_hi(c)[1][i] - geo.lo_hi(c)[0][i], 12) for c in comps]
            s, mixed = _common(sizes)
            self.size[i].setEnabled(True)
            self.size[i].set_value(None if s is None else u.length_to_display(s), mixed)
        hidden = all(n in m["hidden"] for n in self.parts)
        locked = all(n in m["locked"] for n in self.parts)
        self.hide_button.setText("Afficher" if hidden else "Masquer")
        self.lock_button.setText("Déverrouiller" if locked else "Verrouiller")
        for f in self.pos + self.size:
            f.edit.setReadOnly(locked)

    # ------------------------------------------------------------------ edits
    def _rename(self) -> None:
        if self._filling or len(self.parts) != 1:
            return
        new = self.name_edit.text().strip()
        old = self.parts[0]
        if new == old:
            return
        err = geo.rename_error(self.ctx.project.get() or {}, old, new)
        self.name_error.setText(f"Nom refusé : {err}" if err else "")
        self.name_error.setVisible(bool(err))
        if not err:
            self.rename_requested.emit(old, new)

    def _material_chosen(self, index: int) -> None:
        v = self.material.itemData(index)
        if v == CUSTOM_MATERIAL:
            self.new_material_requested.emit()
            self.refresh()
        elif v == EDIT_MATERIAL:
            self.edit_material_requested.emit(self.material.itemText(index)[9:-1])
            self.refresh()
        elif v:
            self.material_requested.emit(v)

    def _tmax(self, v) -> None:
        self.tmax_requested.emit(None if v is None else
                                 self.ctx.units.temperature_from_display(v))

    def _rest_chosen(self, index: int) -> None:
        v = self.rest_on.itemData(index)
        if v is None or v == FLOAT_SUPPORT:
            return
        self.rest_on_requested.emit(v)

    def _pos(self, axis: int, v) -> None:
        if v is not None:
            self.position_requested.emit(axis, self.ctx.units.length_from_display(v))

    def _size(self, axis: int, v) -> None:
        if v is None or v <= 0:
            self.refresh()
            return
        m = self.ctx.units.length_from_display(v)
        if self.parts:
            self.size_requested.emit(axis, m)
        else:
            self.enclosure_requested.emit(axis, m)


def check_lines(data: dict) -> list[tuple[str, str]]:
    """(tone, text) lines of the "Contrôles du modèle" card besides the alerts."""
    out = []
    if not geo.outside(data):
        out.append(("success", "Toutes les pièces sont dans l’enceinte"))
    if not geo.overlaps(data):
        out.append(("success", "Aucune pièce ne se chevauche"))
    total, n = geo.total_power(data)
    out.append(("info", f"Puissance totale : {total:g} W sur {n} source{'s' if n > 1 else ''}"
                if n else "Aucune source de chaleur pour l’instant"))
    return out


class ChecksView(QWidget):
    """Alerts of the step (the case validation's too, with Sélectionner) and the
    checks that pass."""

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(6)
        ctx.alerts.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        while self.lay.count():
            w = self.lay.takeAt(0).widget()
            if w is not None:
                w.hide()  # deleteLater alone leaves it drawn until the next event loop
                w.setParent(None)
                w.deleteLater()
        data = self.ctx.project.get() or {}
        alerts = self.ctx.alerts.for_tab("model")
        self.alert_rows = []
        for a in alerts:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(a.message)
            lab.setWordWrap(True)
            color = COLORS["error_text"] if a.level == "error" else COLORS["warning_text"]
            lab.setStyleSheet(f"color: {color}; font-weight: 600;")
            rl.addWidget(lab, 1)
            if a.target:
                b = QPushButton("Sélectionner")
                b.clicked.connect(lambda _=False, t=a.target: self.ctx.selection.set([t]))
                rl.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
            self.lay.addWidget(row)
            self.alert_rows.append(row)
        self.lines = []
        for tone, text in check_lines(data):
            lab = QLabel(("✓ " if tone == "success" else "") + text)
            lab.setWordWrap(True)
            lab.setStyleSheet(f"color: {COLORS['success'] if tone == 'success' else COLORS['muted']};")
            self.lay.addWidget(lab)
            self.lines.append(text)
        self.lay.addStretch(1)
