"""Inspectors of the Air tab's right panel: an opening (flow, fan, grille, placement)
and an enclosure face (its openings in 2D, its wall).

Owned by: thread "Air". Every widget writes through the tab (``tab.edit_opening``...),
which makes one undo step per change (merged while typing or dragging), and reads back
from the project in ``refresh()`` so undo/redo and edits made elsewhere show up.
"""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from superqt import QLabeledDoubleSlider

from ....case import Grille
from ...theme import set_prop
from .face_editor import FaceEditor
from .fan_chart import FanChart
from .fields import Segmented, combo, form, muted, pair, section, set_value, spin
from .geometry import FACE_LABELS, NAME_RE, face_axis_names, face_size, openings_on
from .ops import (
    EXPERT_MODES,
    INLET_MODES,
    M3H,
    OUTLET_MODES,
    find_opening,
    set_drive,
    set_fan_spec,
    set_grille,
    set_kind,
    wall_for_face,
)
from .walls import FACE_CHOICES, WallForm, face_choice


def _fr(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


class LengthSpins:
    """Spin boxes in display units for metre values; rebuilt when the units change."""

    def __init__(self, units) -> None:
        self.units = units
        self.spins = []

    def make(self, on_change, tip: str = ""):
        s = spin(0, 1e6, 1, self.units.length_symbol, 1.0, tip)
        s.valueChanged.connect(lambda v: on_change(self.units.length_from_display(v)))
        self.spins.append(s)
        self.apply_units()
        return s

    def apply_units(self) -> None:
        mm = self.units.length == "mm"
        for s in self.spins:
            s.blockSignals(True)
            s.setDecimals(1 if mm else 4)
            s.setSingleStep(1.0 if mm else 0.001)
            s.setSuffix(f" {self.units.length_symbol}")
            s.blockSignals(False)

    def set(self, s, metres: float) -> None:
        set_value(s, self.units.length_to_display(metres))


class OpeningInspector(QWidget):
    PAGES: ClassVar[dict[str, str]] = {"opening": "Ouverture", "fan": "Ventilateur", "grille": "Grille"}

    def __init__(self, tab, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tab = tab
        self.ctx = tab.ctx
        self.name: str | None = None
        self.lengths = LengthSpins(self.ctx.units)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        head = form()
        self.name_edit = QLineEdit()
        self.name_edit.setToolTip("Nom de l’ouverture (lettres, chiffres et _)")
        self.name_edit.editingFinished.connect(self._rename)
        head.addRow("Nom", self.name_edit)
        self.kind = Segmented({"inlet": "Entrée d’air", "outlet": "Sortie d’air"})
        self.kind.changed.connect(self._kind)
        head.addRow("Type", self.kind)
        self.delete = QPushButton("Supprimer")
        set_prop(self.delete, kind="danger")
        self.delete.setToolTip("Supprimer l’ouverture (Suppr)")
        self.delete.clicked.connect(lambda: self.name and self.tab.delete_opening(self.name))
        lay.addLayout(head)

        self.pages = Segmented(self.PAGES)
        self.pages.changed.connect(self.show_page)
        lay.addWidget(self.pages)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_opening_page())
        self.stack.addWidget(self._build_fan_page())
        self.stack.addWidget(self._build_grille_page())
        lay.addWidget(self.stack, 1)
        lay.addWidget(self.delete, 0, Qt.AlignmentFlag.AlignRight)
        self.show_page("opening")
        self.ctx.units.changed.connect(self._units_changed)
        self.ctx.expert.changed.connect(self._expert_changed)

    # ------------------------------------------------------------------ pages
    def _build_opening_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(section("Écoulement"))
        f = form()
        self.mode = combo({})
        self.mode.activated.connect(lambda _i: self._edit("Changer l’écoulement",
                                                          lambda o: set_drive(o, self.mode.currentData())))
        f.addRow("Débit par", self.mode)
        self.flow = spin(0.001, 1e5, 2, "m³/h", 1.0)
        self.flow.valueChanged.connect(lambda v: self._set_field("flow_rate", v / M3H, "Débit"))
        f.addRow("Débit", self.flow)
        self.velocity = spin(0.001, 100, 2, "m/s", 0.1)
        self.velocity.valueChanged.connect(lambda v: self._set_field("velocity", v, "Vitesse"))
        f.addRow("Vitesse", self.velocity)
        self.t_ambient = QCheckBox("Air à la température ambiante")
        self.t_ambient.toggled.connect(self._t_toggled)
        f.addRow("Température", self.t_ambient)
        self.temperature = spin(-60, 300, 1, "°C", 1.0)
        self.temperature.valueChanged.connect(
            lambda v: self._set_field("temperature", self.ctx.units.temperature_from_display(v),
                                      "Température d’entrée"))
        f.addRow("", self.temperature)
        self.flow_info = muted()
        f.addRow("", self.flow_info)
        self.flow_form = f
        lay.addLayout(f)

        lay.addWidget(section("Placement"))
        p = form()
        self.face = combo(FACE_LABELS)
        self.face.activated.connect(lambda _i: self.tab.move_opening_to_face(
            self.name, self.face.currentData()))
        p.addRow("Face", self.face)
        self.cu = self.lengths.make(lambda v: self._set_center(0, v), "Position du centre")
        self.cv = self.lengths.make(lambda v: self._set_center(1, v), "Position du centre")
        self.su = self.lengths.make(lambda v: self._set_size(0, v), "Largeur")
        self.sv = self.lengths.make(lambda v: self._set_size(1, v), "Hauteur")
        p.addRow("Centre", pair(self.cu, self.cv))
        p.addRow("Taille", pair(self.su, self.sv))
        self.center_btn = QPushButton("Centrer sur la face")
        set_prop(self.center_btn, kind="flat")
        self.center_btn.clicked.connect(lambda: self.name and self.tab.center_opening(self.name))
        p.addRow("", self.center_btn)
        lay.addLayout(p)
        self.editor = FaceEditor(self.ctx.units)
        self.editor.picked.connect(lambda n: n and self.tab.select_opening(n))
        self.editor.geometry_edited.connect(self.tab.drag_opening)
        lay.addWidget(self.editor)
        self.editor_note = muted()
        lay.addWidget(self.editor_note)
        lay.addStretch(1)
        return w

    def _build_fan_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.has_fan = QCheckBox("Ventilateur sur cette ouverture")
        self.has_fan.toggled.connect(self._fan_toggled)
        lay.addWidget(self.has_fan)
        self.fan_body = QWidget()
        fl = QVBoxLayout(self.fan_body)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        self.fan_spec = Segmented({"curve": "Courbe débit-pression", "flow": "Débit fixe"})
        self.fan_spec.changed.connect(lambda k: self._edit("Type de ventilateur",
                                                           lambda o: set_fan_spec(o, k)))
        fl.addWidget(self.fan_spec)
        f = form()
        self.fan_flow = spin(0.001, 1e5, 2, "m³/h", 1.0)
        self.fan_flow.valueChanged.connect(self._fan_flow)
        f.addRow("Débit", self.fan_flow)
        fl.addLayout(f)
        self.fan_form = f
        self.curve_box = QWidget()
        cl = QVBoxLayout(self.curve_box)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(section("Courbe du constructeur"))
        self.curve = QTableWidget(0, 2)
        self.curve.setHorizontalHeaderLabels(["Débit (m³/h)", "Pression (Pa)"])
        self.curve.verticalHeader().setVisible(False)
        self.curve.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.curve.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.curve.itemChanged.connect(self._curve_edited)
        cl.addWidget(self.curve)
        row = QHBoxLayout()
        add = QPushButton("+ Point")
        add.clicked.connect(self._add_point)
        rem = QPushButton("− Point")
        rem.clicked.connect(self._remove_point)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        cl.addLayout(row)
        cl.addWidget(muted("Débit croissant, pression décroissante : du point de blocage "
                           "(débit nul) au débit libre (pression nulle)."))
        fl.addWidget(self.curve_box)
        fl.addWidget(section("Point de fonctionnement"))
        self.chart = FanChart()
        fl.addWidget(self.chart)
        lay.addWidget(self.fan_body)
        lay.addStretch(1)
        return w

    def _build_grille_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.has_grille = QCheckBox("Grille, paroi perforée ou filtre")
        self.has_grille.toggled.connect(
            lambda on: self._edit("Grille", lambda o: set_grille(o, on)))
        lay.addWidget(self.has_grille)
        self.grille_body = QWidget()
        gl = QVBoxLayout(self.grille_body)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.addWidget(section("Taux d’ouverture"))
        self.open_area = QLabeledDoubleSlider(Qt.Orientation.Horizontal)
        self.open_area.setRange(5, 100)
        self.open_area.setDecimals(0)
        self.open_area.setSingleStep(5)
        self.open_area.valueChanged.connect(self._open_area)
        gl.addWidget(self.open_area)
        gl.addWidget(muted("Part de la surface laissée libre (trous, fentes). 60 à 80 % pour "
                           "une grille de ventilateur, 30 à 50 % pour une tôle perforée."))
        f = form()
        self.k_custom = QCheckBox("Coefficient de perte imposé")
        self.k_custom.toggled.connect(self._k_toggled)
        f.addRow("", self.k_custom)
        self.k = spin(0, 1e4, 2, "", 0.5, "Perte de charge = K × ρ u² / 2")
        self.k.valueChanged.connect(self._k_value)
        f.addRow("K", self.k)
        gl.addLayout(f)
        self.grille_form = f
        self.grille_info = muted()
        gl.addWidget(self.grille_info)
        lay.addWidget(self.grille_body)
        lay.addStretch(1)
        return w

    def show_page(self, key: str) -> None:
        self.pages.set_current(key)
        self.stack.setCurrentIndex(list(self.PAGES).index(key))

    # ------------------------------------------------------------------ edits
    def _edit(self, label: str, fn, merge_key: str | None = None) -> None:
        if self.name and not self._loading:
            self.tab.edit_opening(self.name, label, fn, merge_key)

    _loading = False

    def _set_field(self, key: str, value, label: str) -> None:
        self._edit(label, lambda o: o.__setitem__(key, value), f"{self.name}:{key}")

    def _set_center(self, i: int, v: float) -> None:
        def fn(o):
            c = list(o["center"])
            c[i] = v
            o["center"] = c

        self._edit("Déplacer l’ouverture", fn, f"move:{self.name}")

    def _set_size(self, i: int, v: float) -> None:
        def fn(o):
            s = list(o["size"])
            s[i] = max(v, 1e-4)
            o["size"] = s

        self._edit("Redimensionner l’ouverture", fn, f"size:{self.name}")

    def _rename(self) -> None:
        new = self.name_edit.text().strip()
        if not self.name or new == self.name:
            return
        if not self.tab.rename_opening(self.name, new):
            set_value(self.name_edit, self.name)

    def _kind(self, kind: str) -> None:
        self._edit("Changer le type d’ouverture", lambda o: set_kind(o, kind))

    def _t_toggled(self, on: bool) -> None:
        amb = self.ctx.project.get("ambient_temperature", default=25.0)
        self._set_field("temperature", None if on else amb, "Température d’entrée")

    def _fan_toggled(self, on: bool) -> None:
        def fn(o):
            if on:
                set_drive(o, "fan")
            else:
                set_drive(o, "pressure" if o.get("kind") == "outlet" else "free")

        self._edit("Ventilateur", fn)

    def _fan_flow(self, v: float) -> None:
        def fn(o):
            o["fan"] = {**(o.get("fan") or {}), "flow_rate": v / M3H, "curve": None}

        self._edit("Débit du ventilateur", fn, f"{self.name}:fan_flow")

    def _curve_points(self) -> list[list[float]] | None:
        pts = []
        for r in range(self.curve.rowCount()):
            try:
                q = float(self.curve.item(r, 0).text().replace(",", "."))
                dp = float(self.curve.item(r, 1).text().replace(",", "."))
            except (AttributeError, ValueError):
                return None
            pts.append([q / M3H, dp])
        return pts

    def _curve_edited(self, _item) -> None:
        if self._loading:
            return
        pts = self._curve_points()
        if pts is None:
            self.ctx.show_message("Courbe : saisissez des nombres.")
            return
        self._edit("Courbe du ventilateur",
                   lambda o: o.__setitem__("fan", {**(o.get("fan") or {}), "curve": pts,
                                                   "flow_rate": None}),
                   f"{self.name}:curve")

    def _add_point(self) -> None:
        pts = self._curve_points() or [[0.0, 20.0]]
        q = pts[-1][0]
        pts.append([q * 1.25 if q > 0 else 0.005, 0.0])
        self._edit("Courbe du ventilateur",
                   lambda o: o.__setitem__("fan", {**(o.get("fan") or {}), "curve": pts,
                                                   "flow_rate": None}))

    def _remove_point(self) -> None:
        pts = self._curve_points() or []
        if len(pts) <= 2:
            self.ctx.show_message("Une courbe a besoin d’au moins 2 points.")
            return
        row = self.curve.currentRow()
        pts.pop(row if 0 <= row < len(pts) else -1)
        self._edit("Courbe du ventilateur",
                   lambda o: o.__setitem__("fan", {**(o.get("fan") or {}), "curve": pts,
                                                   "flow_rate": None}))

    def _open_area(self, v: float) -> None:
        self._edit("Taux d’ouverture de la grille",
                   lambda o: o.__setitem__("grille", {**(o.get("grille") or {}),
                                                      "open_area": round(v) / 100}),
                   f"{self.name}:open_area")

    def _k_toggled(self, on: bool) -> None:
        def fn(o):
            g = dict(o.get("grille") or {"open_area": 0.5})
            g["loss_coefficient"] = Grille(open_area=g.get("open_area", 0.5)).k() if on else None
            o["grille"] = g

        self._edit("Coefficient de la grille", fn)

    def _k_value(self, v: float) -> None:
        self._edit("Coefficient de la grille",
                   lambda o: o.__setitem__("grille", {**(o.get("grille") or {}),
                                                      "loss_coefficient": v}),
                   f"{self.name}:k")

    def _units_changed(self) -> None:
        self.lengths.apply_units()
        self.refresh()

    # ------------------------------------------------------------------ refresh
    def set_opening(self, name: str | None) -> None:
        if name != self.name:
            self.name = name
        self.refresh()

    def _expert_changed(self, _on: bool) -> None:
        self.refresh()

    def refresh(self) -> None:
        data = self.ctx.project.data
        o = find_opening(data, self.name) if self.name else None
        if o is None:
            return
        self._loading = True
        try:
            self._refresh(data, o)
        finally:
            self._loading = False

    def _refresh(self, data: dict, o: dict) -> None:
        from .ops import drive_mode, imposed_flow

        expert = self.ctx.expert.on
        inlet = o.get("kind") == "inlet"
        set_value(self.name_edit, o.get("name"))
        self.kind.set_current(o.get("kind"))
        mode = drive_mode(o)
        modes = INLET_MODES if inlet else OUTLET_MODES
        self.mode.blockSignals(True)
        self.mode.clear()
        for k, label in modes.items():
            if k in EXPERT_MODES and not expert and mode != k:
                continue
            self.mode.addItem(label, k)
        self.mode.blockSignals(False)
        set_value(self.mode, mode)
        f = self.flow_form
        f.setRowVisible(self.flow, mode == "flow")
        f.setRowVisible(self.velocity, mode == "velocity")
        f.setRowVisible(self.t_ambient, inlet)
        f.setRowVisible(self.temperature, inlet and o.get("temperature") is not None)
        if o.get("flow_rate") is not None:
            set_value(self.flow, float(o["flow_rate"]) * M3H)
        if o.get("velocity") is not None:
            set_value(self.velocity, float(o["velocity"]))
        set_value(self.t_ambient, o.get("temperature") is None)
        if o.get("temperature") is not None:
            set_value(self.temperature,
                      self.ctx.units.temperature_to_display(float(o["temperature"])))
        self.temperature.setSuffix(f" {self.ctx.units.temperature_symbol}")
        q = imposed_flow(o)
        info = ""
        a = float(o["size"][0]) * float(o["size"][1]) if o.get("size") else 0.0
        if q is not None and a > 0:
            info = f"{_fr(q * M3H)} m³/h · {_fr(q / a, 2)} m/s dans l’ouverture"
        elif mode == "fan":
            info = "Le débit vient de la courbe du ventilateur (onglet Ventilateur)."
        elif mode in ("free", "pressure"):
            info = "Le débit s’établit librement selon les autres ouvertures."
        self.flow_info.setText(info)

        face = o.get("face", "xmin")
        set_value(self.face, face)
        au, av = face_axis_names(face)
        for s, axis, what in ((self.cu, au, "centre"), (self.cv, av, "centre"),
                              (self.su, au, "taille"), (self.sv, av, "taille")):
            s.setPrefix(f"{axis} ")
            s.setToolTip(f"{what.capitalize()} selon {axis}")
        w, h = face_size(data, face)
        for s, lim in ((self.cu, w), (self.su, w), (self.cv, h), (self.sv, h)):
            s.setMaximum(self.ctx.units.length_to_display(max(lim, 1e-6)) * 1.0)
        self.lengths.set(self.cu, float(o["center"][0]))
        self.lengths.set(self.cv, float(o["center"][1]))
        self.lengths.set(self.su, float(o["size"][0]))
        self.lengths.set(self.sv, float(o["size"][1]))
        self.editor.set_state(data, face, o.get("name"))
        n = len(openings_on(data, face))
        self.editor_note.setText(f"Cotes en {self.ctx.units.length_symbol} jusqu’aux bords · "
                                 "glissez pour déplacer, coins pour redimensionner · accroche "
                                 "aux bords, au centre" + (" et aux autres ouvertures" if n > 1
                                                          else ""))

        fan = o.get("fan") or None
        set_value(self.has_fan, fan is not None)
        self.fan_body.setVisible(fan is not None)
        if fan is not None:
            spec = "curve" if fan.get("curve") else "flow"
            self.fan_spec.set_current(spec)
            self.fan_form.setRowVisible(self.fan_flow, spec == "flow")
            self.curve_box.setVisible(spec == "curve")
            if fan.get("flow_rate"):
                set_value(self.fan_flow, float(fan["flow_rate"]) * M3H)
            if fan.get("curve"):
                self._fill_curve(fan["curve"])
            self.chart.show_case(self.ctx.project.case, o["name"])

        g = o.get("grille") or None
        set_value(self.has_grille, g is not None)
        self.grille_body.setVisible(g is not None)
        if g is not None:
            oa = float(g.get("open_area") or 0.5)
            self.open_area.blockSignals(True)
            self.open_area.setValue(oa * 100)
            self.open_area.blockSignals(False)
            k_given = g.get("loss_coefficient") is not None
            self.grille_form.setRowVisible(self.k_custom, expert or k_given)
            self.grille_form.setRowVisible(self.k, k_given)
            set_value(self.k_custom, k_given)
            if k_given:
                set_value(self.k, float(g["loss_coefficient"]))
            self.open_area.setEnabled(not k_given)
            self.grille_info.setText(self._grille_text(o, g))
        self.pages.buttons["fan"].setText("Ventilateur ●" if fan else "Ventilateur")
        self.pages.buttons["grille"].setText("Grille ●" if g else "Grille")

    def _grille_text(self, o: dict, g: dict) -> str:
        try:
            k = Grille.model_validate({k: v for k, v in g.items() if v is not None}).k()
        except Exception:  # noqa: BLE001
            return ""
        text = f"Coefficient de perte K ≈ {_fr(k)}"
        case = self.ctx.project.case
        if case is not None:
            from ....fans import air_density, estimate_flow, opening_velocity

            op = next((p for p in case.openings if p.name == o.get("name")), None)
            if op is not None:
                u = opening_velocity(case, op, estimate_flow(case))
                dp = k * 0.5 * air_density(case) * u * u
                text += f" · perte ≈ {_fr(dp)} Pa à {_fr(u, 2)} m/s (estimation)"
        return text

    def _fill_curve(self, curve) -> None:
        current = self._curve_points()
        if current is not None and len(current) == len(curve) and all(
                abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9
                for a, b in zip(current, curve, strict=True)):
            return
        self.curve.blockSignals(True)
        self.curve.setRowCount(len(curve))
        for r, (q, dp) in enumerate(curve):
            for c, v in ((0, _fr(float(q) * M3H, 2)), (1, _fr(float(dp), 1))):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.curve.setItem(r, c, item)
        self.curve.blockSignals(False)
        rows = sum(self.curve.rowHeight(r) for r in range(self.curve.rowCount()))
        self.curve.setFixedHeight(self.curve.horizontalHeader().height() + rows + 4)


class FaceInspector(QWidget):
    """One enclosure face: its openings in 2D, add buttons, and its wall."""

    def __init__(self, tab, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tab = tab
        self.ctx = tab.ctx
        self.face = "xmin"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.title = muted()
        lay.addWidget(self.title)
        row = QHBoxLayout()
        row.setSpacing(2)
        for label, kind, fan, tip in (("+ Entrée", "inlet", False, "I"),
                                      ("+ Sortie", "outlet", False, "O"),
                                      ("+ Ventilateur", "inlet", True, "N")):
            b = QToolButton()
            b.setText(label)
            b.setToolTip(f"Ajouter au centre de cette face ({tip} puis clic pour choisir "
                         "l’endroit)")
            b.clicked.connect(lambda _=False, k=kind, f=fan:
                              self.tab.create_opening(k, self.face, f))
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.editor = FaceEditor(self.ctx.units)
        self.editor.picked.connect(lambda n: n and self.tab.select_opening(n))
        self.editor.geometry_edited.connect(self.tab.drag_opening)
        self.editor.create_requested.connect(
            lambda uv: self.tab.create_from_editor(self.face, uv))
        lay.addWidget(self.editor)
        lay.addWidget(muted("Choisissez un outil (Entrée I, Sortie O, Ventilateur N) puis "
                            "cliquez sur la face pour placer l’ouverture à cet endroit."))
        lay.addWidget(section("Paroi de cette face"))
        self.choice = combo(FACE_CHOICES)
        self.choice.activated.connect(
            lambda _i: self.tab.set_face_wall(self.face, self.choice.currentData()))
        lay.addWidget(self.choice)
        self.wall = WallForm(self.ctx, "face")
        self.wall.edited.connect(lambda v, mk: self.tab.set_face_wall(self.face, "custom", v, mk))
        lay.addWidget(self.wall)
        lay.addStretch(1)
        self.ctx.expert.changed.connect(self._expert_changed)

    def set_face(self, face: str) -> None:
        self.face = face
        self.wall.key = f"face:{face}"
        self.refresh()

    def _expert_changed(self, _on: bool) -> None:
        self.refresh()

    def refresh(self) -> None:
        data = self.ctx.project.data
        w, h = face_size(data, self.face)
        u = self.ctx.units
        n = len(openings_on(data, self.face))
        self.title.setText(f"{FACE_LABELS[self.face]} · {u.fmt_length(w, 0)} × "
                           f"{u.fmt_length(h, 0)} · {n} ouverture{'s' if n > 1 else ''}")
        self.editor.set_state(data, self.face, self.tab.current_opening())
        self.editor.set_armed(self.tab.armed)
        override = ((data.get("walls") or {}).get("faces") or {}).get(self.face)
        choice = face_choice(override)
        set_value(self.choice, choice)
        self.wall.setVisible(choice == "custom")
        self.wall.set_wall(wall_for_face(data, self.face))


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))
