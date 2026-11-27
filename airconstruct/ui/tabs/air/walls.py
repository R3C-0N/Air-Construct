"""Enclosure walls: one setting for every face, with per-face overrides.

Owned by: thread "Air". Edits ``data["walls"]`` (``airconstruct.case.Walls``): mode
adiabatic or exchange; for exchange the wall material and thickness, the outside
convection coefficient, the outside temperature (default ambient) and the emissivity.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QVBoxLayout, QWidget

from ....materials import SOLIDS
from .fields import combo, form, muted, narrow, section, set_value, spin
from .geometry import FACE_LABELS, FACES
from .ops import H_PRESETS, WALL_DEFAULTS, WALL_MODES, wall_for_face, wall_summary

MATERIAL_LABELS = {"aluminium": "Aluminium", "steel": "Acier", "copper": "Cuivre",
                   "plastic_abs": "Plastique ABS", "fr4": "FR4 (époxy)", "silicon": "Silicium"}
FACE_CHOICES = {"inherit": "Comme les autres parois", "adiabatic": "Adiabatique",
                "custom": "Réglage propre"}


class WallForm(QWidget):
    """Edits one WallThermal dict; ``edited(dict, merge_key)`` on every user change."""

    edited = Signal(dict, str)

    def __init__(self, ctx, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.key = key
        self.value: dict = dict(WALL_DEFAULTS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        f = form()
        self.mode = combo(WALL_MODES)
        self.mode.currentIndexChanged.connect(lambda _i: self._set("mode", self.mode.currentData()))
        f.addRow("Thermique", self.mode)
        self.material = narrow(QComboBox())
        for k in SOLIDS:
            kappa = SOLIDS[k].kappa
            self.material.addItem(f"{MATERIAL_LABELS.get(k, k)} · k {kappa:g} W/m·K", k)
        self.material.currentIndexChanged.connect(
            lambda _i: self._set("material", self.material.currentData()))
        f.addRow("Matériau", self.material)
        self.thickness = spin(0.01, 100, 2, "mm", 0.5)
        self.thickness.valueChanged.connect(lambda v: self._set("thickness", v / 1000))
        f.addRow("Épaisseur", self.thickness)
        self.h = spin(0.5, 500, 1, "W/m²K", 1.0,
                      "Coefficient d’échange par convection à l’extérieur du boîtier")
        self.h.valueChanged.connect(lambda v: self._set("h_external", v))
        f.addRow("Échange ext.", self.h)
        self.h_preset = narrow(QComboBox())
        self.h_preset.addItem("Choisir un cas type…", None)
        for v, label in H_PRESETS:
            self.h_preset.addItem(f"{label} · {v:g} W/m²K", v)
        self.h_preset.activated.connect(self._preset)
        f.addRow("", self.h_preset)
        self.t_ambient = QCheckBox("Température extérieure = ambiante")
        self.t_ambient.toggled.connect(self._t_toggled)
        f.addRow("", self.t_ambient)
        self.t_ext = spin(-60, 200, 1, "°C", 1.0)
        self.t_ext.valueChanged.connect(lambda v: self._set("t_external", v))
        f.addRow("T extérieure", self.t_ext)
        self.emissivity = spin(0, 1, 2, "", 0.05,
                               "Émissivité de la face extérieure (0 = pas de rayonnement, "
                               "0,9 = peinture mate)")
        self.emissivity.valueChanged.connect(lambda v: self._set("emissivity", v))
        f.addRow("Émissivité", self.emissivity)
        lay.addLayout(f)
        self._rows = f
        self.resistance = muted()
        lay.addWidget(self.resistance)

    def _set(self, k: str, v) -> None:
        if self.value.get(k) == v:
            return
        self.value = {**self.value, k: v}
        self._sync_enabled()
        self.edited.emit(dict(self.value), f"{self.key}:{k}")

    def _preset(self, _i: int) -> None:
        v = self.h_preset.currentData()
        if v is not None:
            self.h.setValue(v)
        self.h_preset.setCurrentIndex(0)

    def _t_toggled(self, on: bool) -> None:
        self._set("t_external", None if on else self.ctx.project.get("ambient_temperature",
                                                                     default=25.0))

    def set_wall(self, value: dict) -> None:
        self.value = {**WALL_DEFAULTS, **(value or {})}
        v = self.value
        set_value(self.mode, v["mode"])
        set_value(self.material, v["material"])
        set_value(self.thickness, float(v["thickness"] or 0.001) * 1000)
        set_value(self.h, float(v["h_external"] or 5.0))
        set_value(self.t_ambient, v["t_external"] is None)
        set_value(self.t_ext, v["t_external"] if v["t_external"] is not None else
                  self.ctx.project.get("ambient_temperature", default=25.0))
        set_value(self.emissivity, float(v["emissivity"] or 0.0))
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        on = self.value.get("mode") == "exchange"
        for w in (self.material, self.thickness, self.h, self.h_preset, self.t_ambient,
                  self.t_ext, self.emissivity):
            self._rows.setRowVisible(w, on)
        self._rows.setRowVisible(self.t_ext, on and self.value.get("t_external") is not None)
        self._rows.setRowVisible(self.emissivity, on and (self.ctx.expert.on
                                                          or float(self.value.get("emissivity")
                                                                   or 0) > 0))
        if on:
            try:
                kappa = SOLIDS[self.value["material"]].kappa
                r = float(self.value["thickness"]) / kappa + 1 / float(self.value["h_external"])
                u = 1 / r
                us = f"{u:.1f}".replace(".", ",")
                self.resistance.setText(f"Coefficient global ≈ {us} W/m²K : environ {us} W "
                                        "sortent par m² et par °C d’écart.")
            except (KeyError, ValueError, ZeroDivisionError, TypeError):
                self.resistance.setText("")
        else:
            self.resistance.setText("Aucune chaleur ne traverse la paroi.")
        self.resistance.setVisible(True)


class WallsInspector(QWidget):
    """Common wall setting and one choice per face."""

    def __init__(self, tab, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tab = tab
        self.ctx = tab.ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(section("Toutes les parois"))
        self.common = WallForm(self.ctx, "walls")
        self.common.edited.connect(self._common_edited)
        lay.addWidget(self.common)
        lay.addWidget(section("Par face"))
        f = form()
        self.face_combos: dict[str, QComboBox] = {}
        self.face_notes: dict[str, QLabel] = {}
        for face in FACES:
            c = combo(FACE_CHOICES)
            c.activated.connect(lambda _i, fc=face: self._face_choice(fc))
            f.addRow(FACE_LABELS[face], c)
            self.face_combos[face] = c
        lay.addLayout(f)
        lay.addWidget(muted("« Réglage propre » : sélectionnez la face dans la vue 3D "
                            "pour la régler (par exemple un fond posé sur un sol isolant)."))
        lay.addStretch(1)
        self.ctx.expert.changed.connect(self._expert_changed)

    def _common_edited(self, value: dict, merge_key: str) -> None:
        def fn(d):
            from .ops import walls

            w = walls(d)
            w.update({k: v for k, v in value.items() if k != "faces"})

        self.ctx.project.edit("Modifier les parois", fn, merge_key=merge_key)

    def _face_choice(self, face: str) -> None:
        choice = self.face_combos[face].currentData()
        self.tab.set_face_wall(face, choice)
        if choice == "custom":
            self.tab.select_face(face)

    def _expert_changed(self, _on: bool) -> None:
        self.refresh()

    def refresh(self) -> None:
        data = self.ctx.project.get() or {}
        w = data.get("walls") or {}
        self.common.set_wall({k: v for k, v in w.items() if k != "faces"})
        faces = w.get("faces") or {}
        for face, c in self.face_combos.items():
            set_value(c, face_choice(faces.get(face)))


def face_choice(override: dict | None) -> str:
    if override is None:
        return "inherit"
    if (override.get("mode") or "adiabatic") == "adiabatic":
        return "adiabatic"
    return "custom"


def walls_summary(data: dict) -> str:
    """One line for the left panel, e.g. "aluminium · 1 mm · h 5 W/m²K (2 faces à part)"."""
    w = data.get("walls") or {}
    text = wall_summary(wall_for_face({"walls": {k: v for k, v in w.items() if k != "faces"}},
                                      "xmin"))
    n = len(w.get("faces") or {})
    if n:
        text += f" · {n} face{'s' if n > 1 else ''} à part"
    return text
