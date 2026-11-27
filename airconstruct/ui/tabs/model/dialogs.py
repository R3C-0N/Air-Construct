"""Dialogs of the Modèle tab: custom material, linear array.

Owned by: thread "Modèle" (see docs/ui-architecture.md).
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ....materials import SOLIDS
from ...theme import COLORS, set_prop
from .fields import NumberField

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class MaterialDialog(QDialog):
    """Create or edit a custom solid: name, conductivity, density, specific heat."""

    def __init__(self, taken: set[str], material: dict | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Modifier le matériau" if material else "Matériau personnalisé")
        self.original = (material or {}).get("name")
        self.taken = (set(taken) | set(SOLIDS)) - {self.original}
        m = material or {}
        lay = QVBoxLayout(self)
        intro = QLabel("Enregistré avec le projet, utilisable par toutes ses pièces.")
        set_prop(intro, role="muted")
        lay.addWidget(intro)
        form = QFormLayout()
        self.name_edit = QLineEdit(str(m.get("name") or ""))
        self.name_edit.setPlaceholderText("ex. pad_thermique")
        self.k = NumberField("W/m·K", minimum=0)
        self.rho = NumberField("kg/m³", minimum=0)
        self.cp = NumberField("J/kg·K", minimum=0)
        for f, key in ((self.k, "kappa"), (self.rho, "rho"), (self.cp, "cp")):
            f.set_value(m.get(key))
        form.addRow("Nom", self.name_edit)
        form.addRow("Conductivité k", self.k)
        form.addRow("Masse volumique ρ", self.rho)
        form.addRow("Chaleur massique cp", self.cp)
        lay.addLayout(form)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {COLORS['error_text']}; font-size: 12px;")
        lay.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                       | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.result_material: dict | None = None

    def check(self) -> dict | None:
        name = self.name_edit.text().strip()
        vals = {"kappa": self.k.value(), "rho": self.rho.value(), "cp": self.cp.value()}
        if not NAME_RE.match(name):
            self.error.setText("Nom : lettres, chiffres et _, en commençant par une lettre")
        elif name in self.taken:
            self.error.setText(f"Le nom « {name} » est déjà utilisé")
        elif any(v is None or v <= 0 for v in vals.values()):
            self.error.setText("k, ρ et cp doivent être des nombres positifs")
        else:
            self.error.setText("")
            return {"name": name, **vals}
        return None

    def _ok(self) -> None:
        self.result_material = self.check()
        if self.result_material is not None:
            self.accept()


class ArrayDialog(QDialog):
    """Linear array: number of copies (total), direction, pitch."""

    def __init__(self, units, default_pitch: dict[int, float], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Réseau linéaire")
        self.units = units
        self.default_pitch = default_pitch
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.count = QSpinBox()
        self.count.setRange(2, 100)
        self.count.setValue(2)
        self.axis = QComboBox()
        self.axis.addItems(["x", "y", "z"])
        self.pitch = NumberField(units.length_symbol, minimum=0)
        self.axis.currentIndexChanged.connect(self._axis_changed)
        form.addRow("Nombre total", self.count)
        form.addRow("Direction", self.axis)
        form.addRow("Pas", self.pitch)
        lay.addLayout(form)
        note = QLabel("Le pas va d’une pièce à la suivante (taille + écart).")
        set_prop(note, role="muted")
        lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._axis_changed(0)

    def _axis_changed(self, axis: int) -> None:
        self.pitch.set_value(self.units.length_to_display(self.default_pitch.get(axis, 0.0)))

    def values(self) -> tuple[int, int, float]:
        """(count, axis, pitch in metres)."""
        p = self.pitch.value() or 0.0
        return self.count.value(), self.axis.currentIndex(), self.units.length_from_display(p)
