"""Case editing form: enclosure, components, openings, ambient and precision.

Owned by: workstream "ui".

Lengths are shown in millimetres, flow rates in m3/h, temperatures in degC; the
Case itself stays in SI units. Fields the form does not show (turbulence model,
expert cell size, ...) are kept from the last case given to :meth:`CaseForm.set_case`.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..case import Case, Face, OpeningKind
from ..materials import SOLIDS
from .run import format_duration  # noqa: F401 - re-exported

MM = 1e-3  # metres per millimetre
M3H = 1.0 / 3600.0  # m3/s per m3/h

PRECISION_NAMES = {1: "Very coarse", 2: "Coarse", 3: "Medium", 4: "Fine", 5: "Very fine"}

COMPONENT_COLUMNS = ["Name", "Material", "x", "y", "z", "dx", "dy", "dz", "Power (W)"]
OPENING_COLUMNS = [
    "Name", "Type", "Face", "Centre u", "Centre v", "Width", "Height", "Flow (m³/h)", "Temp (°C)",
]


class FormError(ValueError):
    """A cell of the form could not be read (e.g. text in a number column)."""


def _num(v: float) -> str:
    return f"{v:.6g}"


class _Table(QTableWidget):
    def __init__(self, columns: list[str], parent: QWidget | None = None) -> None:
        super().__init__(0, len(columns), parent)
        self.setHorizontalHeaderLabels(columns)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setStretchLastSection(True)
        self.setMinimumHeight(120)

    def text(self, row: int, col: int) -> str:
        w = self.cellWidget(row, col)
        if isinstance(w, QComboBox):
            return w.currentText()
        item = self.item(row, col)
        return item.text().strip() if item else ""

    def number(self, row: int, col: int, scale: float = 1.0, optional: bool = False) -> float | None:
        t = self.text(row, col).replace(",", ".")
        if not t:
            if optional:
                return None
            raise FormError(f"{self.horizontalHeaderItem(col).text()} is empty (row {row + 1})")
        try:
            return float(t) * scale
        except ValueError:
            raise FormError(
                f"{self.horizontalHeaderItem(col).text()} must be a number (row {row + 1}): {t!r}"
            ) from None

    def set_text(self, row: int, col: int, text: str) -> None:
        self.setItem(row, col, QTableWidgetItem(text))

    def set_combo(self, row: int, col: int, choices: list[str], value: str, on_change) -> None:
        cb = QComboBox()
        cb.addItems(choices)
        cb.setCurrentText(value)
        cb.currentTextChanged.connect(lambda _t: on_change())
        self.setCellWidget(row, col, cb)

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.selectedIndexes()}, reverse=True)
        if not rows and self.rowCount():
            rows = [self.rowCount() - 1]
        for r in rows:
            self.removeRow(r)


class CaseForm(QWidget):
    """Edits a :class:`Case`. Emits ``changed`` whenever the user edits something."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._base: Case | None = None
        self._loading = False

        root = QVBoxLayout(self)

        # --- general ------------------------------------------------------
        general = QGroupBox("Case")
        gl = QFormLayout(general)
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._emit)
        gl.addRow("Name", self.name_edit)
        size_row = QHBoxLayout()
        self.size_spins: list[QDoubleSpinBox] = []
        for axis in "xyz":
            sp = QDoubleSpinBox()
            sp.setRange(1.0, 100000.0)
            sp.setDecimals(1)
            sp.setSuffix(" mm")
            sp.setPrefix(f"{axis}: ")
            sp.valueChanged.connect(self._emit)
            self.size_spins.append(sp)
            size_row.addWidget(sp)
        gl.addRow("Enclosure", size_row)
        self.ambient_spin = QDoubleSpinBox()
        self.ambient_spin.setRange(-50.0, 150.0)
        self.ambient_spin.setSuffix(" °C")
        self.ambient_spin.valueChanged.connect(self._emit)
        gl.addRow("Ambient temperature", self.ambient_spin)
        root.addWidget(general)

        # --- components -----------------------------------------------------
        comp_box = QGroupBox("Components (positions and sizes in mm)")
        cl = QVBoxLayout(comp_box)
        self.components = _Table(COMPONENT_COLUMNS)
        self.components.itemChanged.connect(self._emit)
        cl.addWidget(self.components)
        cl.addLayout(self._buttons(self._add_component_clicked, self.components))
        root.addWidget(comp_box)

        # --- openings -------------------------------------------------------
        open_box = QGroupBox("Openings: fans / vents (centre and size in mm, on the face)")
        ol = QVBoxLayout(open_box)
        self.openings = _Table(OPENING_COLUMNS)
        self.openings.itemChanged.connect(self._emit)
        ol.addWidget(self.openings)
        hint = QLabel(
            "u, v are the face's in-plane axes: x/y faces use (y, z) or (x, z), z faces use (x, y). "
            "Flow is required for inlets; leave Temp empty to use the ambient temperature."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        ol.addWidget(hint)
        ol.addLayout(self._buttons(self._add_opening_clicked, self.openings))
        root.addWidget(open_box)

        # --- precision ------------------------------------------------------
        prec = QGroupBox("Precision")
        pl = QFormLayout(prec)
        self.precision_slider = QSlider(Qt.Orientation.Horizontal)
        self.precision_slider.setRange(1, 5)
        self.precision_slider.setPageStep(1)
        self.precision_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.precision_slider.setTickInterval(1)
        self.precision_slider.valueChanged.connect(self._precision_moved)
        self.precision_label = QLabel()
        prow = QHBoxLayout()
        prow.addWidget(self.precision_slider, 1)
        prow.addWidget(self.precision_label)
        pl.addRow("Mesh", prow)
        self.estimate_label = QLabel("Estimated time: –")
        pl.addRow("", self.estimate_label)
        self.iterations_spin = QSpinBox()
        self.iterations_spin.setRange(10, 1000000)
        self.iterations_spin.setToolTip("Upper limit; runs stop earlier once converged.")
        self.iterations_spin.setSingleStep(100)
        self.iterations_spin.valueChanged.connect(self._emit)
        pl.addRow("Iteration cap", self.iterations_spin)
        self.cores_spin = QSpinBox()
        self.cores_spin.setRange(1, 256)
        self.cores_spin.valueChanged.connect(self._emit)
        pl.addRow("CPU cores", self.cores_spin)
        root.addWidget(prec)
        root.addStretch(1)

    # ------------------------------------------------------------------ helpers
    def _buttons(self, on_add: Callable[[], None], table: _Table) -> QHBoxLayout:
        row = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(on_add)
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: (table.remove_selected(), self._emit()))
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        return row

    def _emit(self, *_args) -> None:
        if not self._loading:
            self.changed.emit()

    def _precision_moved(self, level: int) -> None:
        self.precision_label.setText(PRECISION_NAMES.get(level, str(level)))
        self._emit()

    def _unique_name(self, table: _Table, prefix: str) -> str:
        used = {table.text(r, 0) for r in range(table.rowCount())}
        used |= {self.components.text(r, 0) for r in range(self.components.rowCount())}
        used |= {self.openings.text(r, 0) for r in range(self.openings.rowCount())}
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    def _enclosure_mm(self) -> tuple[float, float, float]:
        return tuple(sp.value() for sp in self.size_spins)  # type: ignore[return-value]

    def _add_component_clicked(self) -> None:
        ex, ey, ez = self._enclosure_mm()
        self.add_component_row(
            self._unique_name(self.components, "part"), "aluminium",
            (ex * 0.4, ey * 0.4, 0.0), (ex * 0.2, ey * 0.2, ez * 0.2), 0.0,
        )
        self._emit()

    def _add_opening_clicked(self) -> None:
        has_inlet = any(
            self.openings.text(r, 1) == "inlet" for r in range(self.openings.rowCount())
        )
        kind = "outlet" if has_inlet else "inlet"
        _ex, ey, ez = self._enclosure_mm()
        face = "xmax" if kind == "outlet" else "xmin"
        self.add_opening_row(
            self._unique_name(self.openings, kind), kind, face,
            (ey / 2, ez / 2), (ey / 2, ez / 2), 20.0 if kind == "inlet" else None, None,
        )
        self._emit()

    # ------------------------------------------------------------------ rows
    def add_component_row(self, name, material, origin_mm, size_mm, power) -> None:
        t = self.components
        r = t.rowCount()
        t.blockSignals(True)
        t.insertRow(r)
        t.set_text(r, 0, name)
        t.set_combo(r, 1, sorted(SOLIDS), material, self._emit)
        for i, v in enumerate(list(origin_mm) + list(size_mm)):
            t.set_text(r, 2 + i, _num(v))
        t.set_text(r, 8, _num(power))
        t.blockSignals(False)

    def add_opening_row(self, name, kind, face, center_mm, size_mm, flow_m3h, temp_c) -> None:
        t = self.openings
        r = t.rowCount()
        t.blockSignals(True)
        t.insertRow(r)
        t.set_text(r, 0, name)
        t.set_combo(r, 1, [k.value for k in OpeningKind], kind, self._emit)
        t.set_combo(r, 2, [f.value for f in Face], face, self._emit)
        for i, v in enumerate(list(center_mm) + list(size_mm)):
            t.set_text(r, 3 + i, _num(v))
        t.set_text(r, 7, "" if flow_m3h is None else _num(flow_m3h))
        t.set_text(r, 8, "" if temp_c is None else _num(temp_c))
        t.blockSignals(False)

    # ------------------------------------------------------------------ I/O
    def set_case(self, case: Case) -> None:
        self._loading = True
        try:
            self._base = case
            self.name_edit.setText(case.name)
            for sp, v in zip(self.size_spins, case.enclosure.as_tuple()):
                sp.setValue(v / MM)
            self.ambient_spin.setValue(case.ambient_temperature)
            self.components.setRowCount(0)
            for c in case.components:
                self.add_component_row(
                    c.name, c.material,
                    [v / MM for v in c.box.origin.as_tuple()],
                    [v / MM for v in c.box.size.as_tuple()],
                    c.power,
                )
            self.openings.setRowCount(0)
            for o in case.openings:
                flow = None
                if o.flow_rate is not None:
                    flow = o.flow_rate / M3H
                elif o.velocity is not None:
                    flow = o.velocity * o.area() / M3H
                self.add_opening_row(
                    o.name, o.kind.value, o.face.value,
                    [v / MM for v in o.center], [v / MM for v in o.size],
                    flow if o.kind == OpeningKind.inlet else None, o.temperature,
                )
            self.precision_slider.setValue(case.precision.level)
            self._precision_moved(case.precision.level)
            self.iterations_spin.setValue(case.solver.iterations)
            self.cores_spin.setValue(case.solver.cores)
        finally:
            self._loading = False

    def to_case(self) -> Case:
        """Build a Case from the form. Raises FormError or pydantic.ValidationError."""
        return Case.model_validate(self.to_data())

    def to_data(self, base: dict | None = None) -> dict:
        """The case data shown by the form, not validated (raises FormError only).
        Keys the form does not show are taken from ``base`` (default: the last case
        given to set_case)."""
        if base is None:
            base = self._base.model_dump(mode="json") if self._base else {}
        data = dict(base)
        data["name"] = self.name_edit.text().strip()
        ex, ey, ez = (v * MM for v in self._enclosure_mm())
        data["enclosure"] = {"x": ex, "y": ey, "z": ez}
        data["ambient_temperature"] = self.ambient_spin.value()

        t = self.components
        comps = []
        for r in range(t.rowCount()):
            o = [t.number(r, c, MM) for c in (2, 3, 4)]
            s = [t.number(r, c, MM) for c in (5, 6, 7)]
            comps.append({
                "name": t.text(r, 0),
                "material": t.text(r, 1),
                "box": {
                    "origin": dict(zip("xyz", o)),
                    "size": dict(zip("xyz", s)),
                },
                "power": t.number(r, 8, optional=True) or 0.0,
            })
        data["components"] = comps

        t = self.openings
        opens = []
        for r in range(t.rowCount()):
            kind = t.text(r, 1)
            flow = t.number(r, 7, M3H, optional=True)
            opens.append({
                "name": t.text(r, 0),
                "kind": kind,
                "face": t.text(r, 2),
                "center": [t.number(r, 3, MM), t.number(r, 4, MM)],
                "size": [t.number(r, 5, MM), t.number(r, 6, MM)],
                "flow_rate": flow if kind == OpeningKind.inlet.value else None,
                "velocity": None,
                "temperature": t.number(r, 8, optional=True),
            })
        data["openings"] = opens

        prec = dict(base.get("precision") or {})
        prec["level"] = self.precision_slider.value()
        data["precision"] = prec
        solver = dict(base.get("solver") or {})
        solver["iterations"] = self.iterations_spin.value()
        solver["cores"] = self.cores_spin.value()
        data["solver"] = solver
        return data

    def set_estimate(self, text: str) -> None:
        self.estimate_label.setText(text)
