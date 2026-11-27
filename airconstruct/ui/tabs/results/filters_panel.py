"""Left panel of the Résultats tab: the list of filters (eye = shown) and the
settings of the selected one.

Owned by: thread "Résultats" (see docs/ui-architecture.md).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSpinBox,
    QToolButton,
    QWidget,
)
from superqt import QLabeledDoubleSlider

from ...theme import set_prop
from ...widgets.card import Card
from .data import fr
from .scene import AXES, Filter, default_line

ADD_ORDER = ("slice", "iso", "vectors", "streamlines", "particles", "line", "parts")
ADD_LABELS = {
    "slice": "Coupe (plan de coupe)", "iso": "Isosurface", "vectors": "Vecteurs sur une coupe",
    "streamlines": "Lignes de courant", "particles": "Particules animées",
    "line": "Ligne de mesure A → B", "parts": "Pièces",
}
AXIS_LABELS = {"x": "x (verticale)", "y": "y (verticale)", "z": "z (horizontale)"}


def _slider(lo: float, hi: float, value: float, decimals: int = 1) -> QLabeledDoubleSlider:
    s = QLabeledDoubleSlider(Qt.Orientation.Horizontal)
    s.setDecimals(decimals)
    s.setRange(lo, hi)
    s.setValue(min(max(value, lo), hi))
    return s


class FiltersPanel(Card):
    """``changed()`` after any edit (the tab rebuilds the scene, debounced);
    ``play_toggled(bool)`` for the particle animation."""

    changed = Signal()
    play_toggled = Signal(bool)

    def __init__(self, tab, parent: QWidget | None = None) -> None:
        super().__init__("Filtres", parent)
        self.tab = tab
        self.filters: list[Filter] = []
        self.list = QListWidget()
        self.list.setMinimumHeight(120)
        self.list.itemChanged.connect(self._item_changed)
        self.list.currentRowChanged.connect(self._show_editor)
        self.body.addWidget(self.list)

        self.add_button = QToolButton()
        self.add_button.setText("+ Ajouter un filtre")
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.add_button)
        for kind in ADD_ORDER:
            menu.addAction(ADD_LABELS[kind], lambda k=kind: self.add(k))
        self.add_button.setMenu(menu)
        self.remove_button = QPushButton("Supprimer")
        set_prop(self.remove_button, kind="danger")
        self.remove_button.clicked.connect(self.remove_current)
        row = QHBoxLayout()
        row.addWidget(self.add_button)
        row.addStretch(1)
        row.addWidget(self.remove_button)
        self.body.addLayout(row)

        self.editor_title = QLabel("")
        set_prop(self.editor_title, role="section")
        self.body.addWidget(self.editor_title)
        self.editor = QWidget()
        self.editor_layout = QFormLayout(self.editor)
        self.editor_layout.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(self.editor)
        self.body.addStretch(1)
        self._filling = False
        self.play_button: QPushButton | None = None
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.setInterval(120)
        self._emit_timer.timeout.connect(self.changed.emit)

    # ---- list --------------------------------------------------------------
    def set_filters(self, filters: list[Filter]) -> None:
        self.filters = filters
        self._fill_list()
        self.list.setCurrentRow(0 if filters else -1)

    def _subtitle(self, f: Filter) -> str:
        u = self.tab.ctx.units
        p = f.params
        if f.kind in ("slice", "vectors"):
            pos = p["position"]
            if pos is None:
                case = self.tab.case
                pos = case.enclosure.as_tuple()[AXES[p["axis"]]] / 2 if case else 0.0
            s = f"{p['axis']} = {u.fmt_length(pos, 0 if u.length == 'mm' else 3)}"
            return s + (f" · {self.tab.field}" if f.kind == "slice" else "")
        if f.kind == "iso":
            v = p["value"]
            return "valeur auto (60 %)" if v is None else f"{self.tab.field} = {fr(v)}"
        if f.kind == "streamlines":
            return f"{p['seeds']} départs · épaisseur {fr(p['thickness'])}"
        if f.kind == "particles":
            return f"{p['count']} traceurs"
        if f.kind == "line":
            return f"A → B · {self.tab.field}"
        return f"couleur : {self.tab.field}"

    def _fill_list(self) -> None:
        self._filling = True
        row = self.list.currentRow()
        self.list.clear()
        for f in self.filters:
            it = QListWidgetItem(f"{f.title}\n{self._subtitle(f)}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if f.visible else Qt.CheckState.Unchecked)
            it.setToolTip("Cochez pour afficher, décochez pour masquer")
            self.list.addItem(it)
        self.list.setCurrentRow(min(row, len(self.filters) - 1))
        self._filling = False

    def refresh_texts(self) -> None:
        self._filling = True
        for i, f in enumerate(self.filters):
            item = self.list.item(i)
            if item is not None:
                item.setText(f"{f.title}\n{self._subtitle(f)}")
        self._filling = False

    def _item_changed(self, item: QListWidgetItem) -> None:
        if self._filling:
            return
        i = self.list.row(item)
        if 0 <= i < len(self.filters):
            self.filters[i].visible = item.checkState() == Qt.CheckState.Checked
            self._changed(now=True)

    def add(self, kind: str) -> Filter:
        f = Filter(kind)
        if kind == "line" and self.tab.case is not None:
            f.params["a"], f.params["b"] = default_line(self.tab.case)
        self.filters.append(f)
        self._fill_list()
        self.list.setCurrentRow(len(self.filters) - 1)
        self._changed(now=True)
        return f

    def remove_current(self) -> None:
        i = self.list.currentRow()
        if 0 <= i < len(self.filters):
            f = self.filters.pop(i)
            if f.kind == "particles":
                self.play_toggled.emit(False)
            self._fill_list()
            self._changed(now=True)

    def current(self) -> Filter | None:
        i = self.list.currentRow()
        return self.filters[i] if 0 <= i < len(self.filters) else None

    def _changed(self, now: bool = False) -> None:
        self.refresh_texts()
        if now:
            self._emit_timer.stop()
            self.changed.emit()
        else:
            self._emit_timer.start()

    # ---- editor ------------------------------------------------------------
    def _clear_editor(self) -> None:
        while self.editor_layout.rowCount():
            self.editor_layout.removeRow(0)
        self.play_button = None

    def _show_editor(self, *_args) -> None:
        self._clear_editor()
        f = self.current()
        self.remove_button.setEnabled(f is not None)
        self.editor_title.setText(f.title.upper() if f else "")
        if f is None:
            return
        case = self.tab.case
        E = case.enclosure.as_tuple() if case else (1.0, 1.0, 1.0)
        u = self.tab.ctx.units
        p = f.params
        form = self.editor_layout

        def set_param(key, value, now=False):
            p[key] = value
            self._changed(now)

        if f.kind in ("slice", "vectors"):
            axis = QComboBox()
            for k, label in AXIS_LABELS.items():
                axis.addItem(label, k)
            axis.setCurrentIndex(list(AXIS_LABELS).index(p["axis"]))
            form.addRow("Normale", axis)
            pos_value = p["position"] if p["position"] is not None else E[AXES[p["axis"]]] / 2
            ext = E[AXES[p["axis"]]]
            pos = _slider(0.0, u.length_to_display(ext), u.length_to_display(pos_value),
                          1 if u.length == "mm" else 4)
            pos.valueChanged.connect(lambda v: set_param("position", u.length_from_display(v)))
            form.addRow(f"Position ({u.length_symbol})", pos)

            def axis_changed(_i):
                p["axis"] = axis.currentData()
                p["position"] = None
                self._show_editor()
                self._changed(now=True)

            axis.currentIndexChanged.connect(axis_changed)
            if f.kind == "vectors":
                dens = QSpinBox()
                dens.setRange(4, 60)
                dens.setValue(int(p["density"]))
                dens.valueChanged.connect(lambda v: set_param("density", v))
                form.addRow("Flèches par côté", dens)
                scale = _slider(0.2, 4.0, p["scale"])
                scale.valueChanged.connect(lambda v: set_param("scale", v))
                form.addRow("Taille", scale)
        elif f.kind == "parts":
            op = _slider(0.1, 1.0, p["opacity"], 2)
            op.valueChanged.connect(lambda v: set_param("opacity", v))
            form.addRow("Opacité", op)
        elif f.kind == "iso":
            lo, hi = self.tab.field_range(air_only=True)
            v = p["value"] if p["value"] is not None else lo + 0.6 * (hi - lo)
            val = _slider(lo, hi, v, 2 if self.tab.field == "U" else 1)
            val.valueChanged.connect(lambda x: set_param("value", x))
            unit = "m/s" if self.tab.field == "U" else "°C"
            form.addRow(f"Valeur ({unit})", val)
            op = _slider(0.1, 1.0, p["opacity"], 2)
            op.valueChanged.connect(lambda x: set_param("opacity", x))
            form.addRow("Opacité", op)
        elif f.kind == "streamlines":
            seeds = QSpinBox()
            seeds.setRange(4, 400)
            seeds.setValue(int(p["seeds"]))
            seeds.valueChanged.connect(lambda v: set_param("seeds", v))
            form.addRow("Départs", seeds)
            th = _slider(0.2, 5.0, p["thickness"])
            th.valueChanged.connect(lambda v: set_param("thickness", v))
            form.addRow("Épaisseur", th)
        elif f.kind == "particles":
            n = QSpinBox()
            n.setRange(10, 2000)
            n.setSingleStep(20)
            n.setValue(int(p["count"]))
            n.valueChanged.connect(lambda v: set_param("count", v))
            form.addRow("Traceurs", n)
            self.play_button = QPushButton("▶  Animer")
            self.play_button.setCheckable(True)
            self.play_button.setChecked(self.tab.playing)
            self.play_button.toggled.connect(self.play_toggled.emit)
            form.addRow(self.play_button)
        elif f.kind == "line":
            if p["a"] is None and case is not None:
                p["a"], p["b"] = default_line(case)
            for key in ("a", "b"):
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(4)
                for i in range(3):
                    sb = QDoubleSpinBox()
                    sb.setDecimals(1 if u.length == "mm" else 4)
                    sb.setRange(0.0, u.length_to_display(E[i]))
                    sb.setValue(u.length_to_display(p[key][i] if p[key] else 0.0))
                    sb.setPrefix("xyz"[i] + " ")

                    def edited(v, k=key, i=i):
                        pt = list(p[k])
                        pt[i] = u.length_from_display(v)
                        set_param(k, pt)

                    sb.valueChanged.connect(edited)
                    rl.addWidget(sb)
                form.addRow(f"{key.upper()} ({u.length_symbol})", row)
            reset = QPushButton("Entrée → sortie")
            reset.setToolTip("Replace la ligne de l’entrée d’air à la sortie")

            def reset_line():
                if case is not None:
                    p["a"], p["b"] = default_line(case)
                    self._show_editor()
                    self._changed(now=True)

            reset.clicked.connect(reset_line)
            form.addRow(reset)
