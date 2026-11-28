"""Side cards of the Simulation step: solver settings, live temperatures and probes, run
queue.

Owned by: thread "Simulation".
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSpinBox,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...run import QueueItem, format_duration
from ...theme import COLORS, set_prop
from ...widgets.card import Card

TURBULENCE = {"kOmegaSST": "k-ω SST", "laminar": "Laminaire"}


def fmt_c(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.1f} °C".replace(".", ",")


# --------------------------------------------------------------------------- solver


class SolverCard(Card):
    """Iteration cap, CPU cores, turbulence model, gravity. Edits go to the project
    (one undo step each); the card is read-only while the model is locked."""

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__("Solveur", parent)
        self.ctx = ctx
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.iterations = QSpinBox()
        self.iterations.setRange(100, 1_000_000)
        self.iterations.setSingleStep(1000)
        self.iterations.setGroupSeparatorShown(True)
        self.iterations.setSuffix(" it.")
        self.cores = QSpinBox()
        self.cores.setRange(1, 256)
        self.turbulence = QComboBox()
        for key, label in TURBULENCE.items():
            self.turbulence.addItem(label, key)
        self.gravity = QCheckBox("Gravité (convection naturelle)")
        self.probe_interval = QSpinBox()
        self.probe_interval.setRange(1, 10_000)
        self.probe_interval.setSuffix(" it.")
        form.addRow("Itérations max", self.iterations)
        form.addRow("Cœurs CPU", self.cores)
        form.addRow("Turbulence", self.turbulence)
        self._probe_row_label = QLabel("Relevé des sondes")
        form.addRow(self._probe_row_label, self.probe_interval)
        self.body.addLayout(form)
        self.body.addWidget(self.gravity)
        self.lock_note = QLabel("Modèle, Air et Volume d’air sont verrouillés pendant le calcul.")
        self.lock_note.setWordWrap(True)
        set_prop(self.lock_note, role="pill", tone="info")
        self.lock_note.setStyleSheet("max-height: 1000px; padding: 6px 8px;")
        self.lock_note.setMinimumWidth(120)
        self.body.addWidget(self.lock_note)
        self._syncing = False
        self.iterations.valueChanged.connect(lambda v: self._set("iterations", int(v)))
        self.cores.valueChanged.connect(lambda v: self._set("cores", int(v)))
        self.turbulence.currentIndexChanged.connect(
            lambda _i: self._set("turbulence", self.turbulence.currentData()))
        self.gravity.toggled.connect(lambda on: self._set("gravity", bool(on)))
        self.probe_interval.valueChanged.connect(lambda v: self._set("probe_interval", int(v)))
        ctx.project.changed.connect(self.sync)
        ctx.project.lock_changed.connect(lambda _l: self.sync())
        ctx.expert.changed.connect(lambda _on: self.sync())
        self.sync()

    def _set(self, key: str, value) -> None:
        if self._syncing:
            return
        if self.ctx.project.get("solver", key) == value:
            return

        def fn(d: dict) -> None:
            d.setdefault("solver", {})[key] = value

        labels = {"iterations": "limite d’itérations", "cores": "cœurs CPU",
                  "turbulence": "turbulence", "gravity": "gravité",
                  "probe_interval": "relevé des sondes"}
        if not self.ctx.project.edit(f"Modifier {labels[key]}", fn, merge_key=f"solver:{key}"):
            self.sync()  # refused (locked): show the value back

    def sync(self) -> None:
        from ....case import Solver

        self._syncing = True
        try:
            default = Solver()
            s = self.ctx.project.get("solver") or {}
            self.iterations.setValue(int(s.get("iterations", default.iterations)))
            self.cores.setValue(int(s.get("cores", default.cores)))
            i = self.turbulence.findData(s.get("turbulence", default.turbulence))
            self.turbulence.setCurrentIndex(max(0, i))
            self.gravity.setChecked(bool(s.get("gravity", default.gravity)))
            self.probe_interval.setValue(int(s.get("probe_interval", default.probe_interval)))
        finally:
            self._syncing = False
        locked = self.ctx.project.locked
        for w in (self.iterations, self.cores, self.turbulence, self.gravity,
                  self.probe_interval):
            w.setEnabled(not locked)
        expert = self.ctx.expert.on
        self.probe_interval.setVisible(expert)
        self._probe_row_label.setVisible(expert)
        self.lock_note.setVisible(locked)


# --------------------------------------------------------------------------- live


class LiveCard(Card):
    """Max temperature of every part during the run, and the probes (placed here)."""

    probe_selected = Signal(str)  # probe name

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__("En direct", parent)
        self.ctx = ctx
        self.state = QLabel("Les températures s’affichent pendant le calcul.")
        self.state.setWordWrap(True)
        set_prop(self.state, role="muted")
        self.parts = QTreeWidget()
        self.parts.setHeaderLabels(["Pièce", "T max"])
        self.parts.setRootIsDecorated(False)
        self.parts.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.parts.setMinimumHeight(110)
        self.parts.header().setStretchLastSection(False)
        self.parts.header().setSectionResizeMode(0, self.parts.header().ResizeMode.Stretch)
        self.parts.header().setSectionResizeMode(1, self.parts.header().ResizeMode.ResizeToContents)

        probes_title = QLabel("SONDES")
        set_prop(probes_title, role="section")
        self.add_probe_button = QToolButton()
        self.add_probe_button.setText("+ Sonde")
        self.add_probe_button.setToolTip("Poser une sonde au centre de la sélection (P)")
        self.remove_probe_button = QToolButton()
        self.remove_probe_button.setText("Retirer")
        head = QHBoxLayout()
        head.addWidget(probes_title, 1)
        head.addWidget(self.add_probe_button)
        head.addWidget(self.remove_probe_button)
        self.probes = QTreeWidget()
        self.probes.setHeaderLabels(["Sonde", "Valeur"])
        self.probes.setRootIsDecorated(False)
        self.probes.setMinimumHeight(70)
        self.probes.header().setStretchLastSection(False)
        self.probes.header().setSectionResizeMode(0, self.probes.header().ResizeMode.Stretch)
        self.probes.header().setSectionResizeMode(1, self.probes.header().ResizeMode.ResizeToContents)

        # coordinates of the selected probe
        self.coords = QWidget()
        cl = QHBoxLayout(self.coords)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)
        self.coord_spins = []
        for axis in "xyz":
            sp = QDoubleSpinBox()
            sp.setDecimals(1)
            sp.setRange(-1e5, 1e5)
            sp.setPrefix(f"{axis} ")
            sp.setMinimumWidth(60)  # three in a narrow side panel
            sp.setKeyboardTracking(False)
            sp.valueChanged.connect(lambda _v, a=axis: self._move_probe(a))
            cl.addWidget(sp)
            self.coord_spins.append(sp)
        self.coords.setVisible(False)

        self.body.addWidget(self.state)
        self.body.addWidget(self.parts, 1)
        self.body.addLayout(head)
        self.body.addWidget(self.probes)
        self.body.addWidget(self.coords)

        self._probe_values: dict[str, tuple[float, float]] = {}
        self._syncing = False
        self.add_probe_button.clicked.connect(self.add_probe)
        self.remove_probe_button.clicked.connect(self.remove_probe)
        self.probes.currentItemChanged.connect(self._on_probe_item)
        ctx.project.changed.connect(self.sync_probes)
        ctx.project.lock_changed.connect(lambda _l: self.sync_probes())
        ctx.units.changed.connect(self.sync_probes)
        ctx.selection.changed.connect(self._on_selection)
        self.sync_probes()

    # ---- parts
    def set_temperatures(self, temps: dict[str, float], note: str = "") -> None:
        """{region: max °C}, hottest first; ``note`` replaces the hint above."""
        self.parts.clear()
        for region, t in sorted(temps.items(), key=lambda kv: -kv[1]
                                if math.isfinite(kv[1]) else math.inf):
            item = QTreeWidgetItem([region, fmt_c(t)])
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setData(0, Qt.ItemDataRole.UserRole, region)
            self.parts.addTopLevelItem(item)
        self.state.setText(note)
        self.state.setVisible(bool(note))

    def part_rows(self) -> list[tuple[str, str]]:
        return [(self.parts.topLevelItem(i).text(0), self.parts.topLevelItem(i).text(1))
                for i in range(self.parts.topLevelItemCount())]

    # ---- probes
    def _probes(self) -> list[dict]:
        return list(self.ctx.project.get("probes") or [])

    def set_probe_values(self, values: dict[str, tuple[float, float]]) -> None:
        """{probe: (°C, m/s)} from probes.read_probes (nan when not sampled yet)."""
        self._probe_values = dict(values)
        self.sync_probes()

    def probe_text(self, name: str) -> str:
        t, s = self._probe_values.get(name, (math.nan, math.nan))
        if not math.isfinite(t):
            return "—"
        text = fmt_c(t)
        if math.isfinite(s):
            text += f" · {s:.1f} m/s".replace(".", ",")
        return text

    def sync_probes(self) -> None:
        self._syncing = True
        try:
            current = self.current_probe()
            self.probes.clear()
            u = self.ctx.units
            for p in self._probes():
                pt = p.get("point") or {}
                where = ", ".join(f"{u.length_to_display(pt.get(a, 0.0)):.0f}" for a in "xyz")
                item = QTreeWidgetItem([f"{p['name']} · ({where})", self.probe_text(p["name"])])
                item.setData(0, Qt.ItemDataRole.UserRole, p["name"])
                item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
                self.probes.addTopLevelItem(item)
                if p["name"] == current:
                    self.probes.setCurrentItem(item)
            locked = self.ctx.project.locked
            self.add_probe_button.setEnabled(not locked)
            self.remove_probe_button.setEnabled(not locked and self.current_probe() is not None)
            self._sync_coords()
        finally:
            self._syncing = False

    def current_probe(self) -> str | None:
        item = self.probes.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def _sync_coords(self) -> None:
        name = self.current_probe()
        probe = next((p for p in self._probes() if p["name"] == name), None)
        self.coords.setVisible(probe is not None)
        if probe is None:
            return
        self.coords.setToolTip(f"Position de la sonde ({self.ctx.units.length_symbol})")
        u = self.ctx.units
        for sp, axis in zip(self.coord_spins, "xyz"):
            sp.blockSignals(True)
            sp.setValue(u.length_to_display(probe["point"].get(axis, 0.0)))
            sp.setEnabled(not self.ctx.project.locked)
            sp.blockSignals(False)

    def _on_probe_item(self, item, _prev) -> None:
        if self._syncing:
            return
        self.remove_probe_button.setEnabled(item is not None and not self.ctx.project.locked)
        self._sync_coords()
        if item is not None:
            name = item.data(0, Qt.ItemDataRole.UserRole)
            self.ctx.selection.set([f"probe:{name}"])
            self.probe_selected.emit(name)

    def _on_selection(self, keys: list) -> None:
        names = [k.split(":", 1)[1] for k in keys if k.startswith("probe:")]
        if not names or names[0] == self.current_probe():
            return
        for i in range(self.probes.topLevelItemCount()):
            it = self.probes.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == names[0]:
                self._syncing = True
                self.probes.setCurrentItem(it)
                self._syncing = False
                self._sync_coords()

    def _move_probe(self, axis: str) -> None:
        name = self.current_probe()
        if name is None:
            return
        sp = self.coord_spins["xyz".index(axis)]
        value = self.ctx.units.length_from_display(sp.value())

        def fn(d: dict) -> None:
            for p in d.get("probes") or []:
                if p["name"] == name:
                    p["point"][axis] = value

        self.ctx.project.edit(f"Déplacer la sonde {name}", fn, merge_key=f"probe:{name}")

    def new_probe_point(self) -> dict:
        """Centre of the selected component (or opening face, or the enclosure)."""
        d = self.ctx.project.data
        enc = d.get("enclosure") or {"x": 0.1, "y": 0.1, "z": 0.1}
        key = self.ctx.selection.current or ""
        kind, _, name = key.partition(":")
        if kind == "component":
            comp = next((c for c in d.get("components") or [] if c["name"] == name), None)
            if comp is not None:
                try:
                    from ....case import Component

                    box = Component.model_validate(comp).box
                    lo, hi = box.min, box.max
                    # just above the part, in the air: where a thermocouple would sit
                    z = min(hi[2] + 0.002, float(enc["z"]) * 0.999)
                    return {"x": (lo[0] + hi[0]) / 2, "y": (lo[1] + hi[1]) / 2, "z": z}
                except Exception:  # noqa: BLE001, S110 - fall back to the centre
                    pass
        if kind == "probe":
            p = next((p for p in d.get("probes") or [] if p["name"] == name), None)
            if p is not None:
                return dict(p["point"])
        return {a: float(enc[a]) / 2 for a in "xyz"}

    def add_probe(self, point: dict | None = None) -> str | None:
        """A new probe at ``point`` ({x, y, z}, metres), by default near the selection
        (:meth:`new_probe_point`). Returns its name, None if refused."""
        if self.ctx.project.locked:
            return None
        names = {p["name"] for p in self._probes()}
        n = 1
        while f"S{n}" in names:
            n += 1
        name = f"S{n}"
        point = dict(point) if point is not None else self.new_probe_point()

        def fn(d: dict) -> None:
            d.setdefault("probes", []).append({"name": name, "point": point})

        if self.ctx.project.edit(f"Poser la sonde {name}", fn):
            self.ctx.selection.set([f"probe:{name}"])
            self._on_selection([f"probe:{name}"])
            return name
        return None

    def remove_probe(self) -> None:
        name = self.current_probe()
        if name is None:
            return
        self.ctx.project.edit(f"Retirer la sonde {name}",
                              lambda d: d.__setitem__("probes", [p for p in d.get("probes") or []
                                                                  if p["name"] != name]))


# --------------------------------------------------------------------------- queue

STATUS_TEXT = {"queued": "en attente", "running": "en cours", "converged": "convergé",
               "finished": "non convergé", "stopped": "arrêté", "failed": "échoué"}
STATUS_TONE = {"running": "info", "converged": "success", "finished": "warning",
               "stopped": "warning", "failed": "error"}


class QueueCard(Card):
    """Cases waiting to run after the current one, and the ones that ended."""

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__("File d’attente", parent)
        self.ctx = ctx
        r = ctx.runner
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Cas", "État"])
        self.list.setRootIsDecorated(False)
        self.list.setMinimumHeight(90)
        self.list.header().setStretchLastSection(False)
        self.list.header().setSectionResizeMode(0, self.list.header().ResizeMode.Stretch)
        self.list.header().setSectionResizeMode(1, self.list.header().ResizeMode.ResizeToContents)
        self.empty = QLabel("Ajoutez des variantes ou d’autres projets : ils se lancent l’un "
                            "après l’autre.")
        self.empty.setWordWrap(True)
        set_prop(self.empty, role="muted")
        self.add_button = QPushButton("+ Ajouter à la file")
        menu = QMenu(self.add_button)
        self.add_current_action = menu.addAction("Le modèle actuel", self.add_current)
        self.add_file_action = menu.addAction("Un fichier de projet…", self.add_file)
        self.add_button.setMenu(menu)
        self.start_button = QPushButton("Lancer la file")
        set_prop(self.start_button, kind="primary")
        self.start_button.clicked.connect(r.start_queue)
        self.up_button = QToolButton()
        self.up_button.setText("↑")
        self.up_button.setToolTip("Monter")
        self.down_button = QToolButton()
        self.down_button.setText("↓")
        self.down_button.setToolTip("Descendre")
        self.remove_button = QToolButton()
        self.remove_button.setText("Retirer")
        self.open_button = QToolButton()
        self.open_button.setText("Ouvrir")
        self.open_button.setToolTip("Ouvrir ce projet avec son résultat")
        tools = QHBoxLayout()
        tools.setSpacing(2)
        for b in (self.up_button, self.down_button, self.remove_button, self.open_button):
            tools.addWidget(b)
        tools.addStretch(1)
        buttons = QVBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.start_button)
        self.body.addWidget(self.empty)
        self.body.addWidget(self.list, 1)
        self.body.addLayout(tools)
        self.body.addLayout(buttons)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.remove_button.clicked.connect(self._remove)
        self.open_button.clicked.connect(self.open_selected)
        self.list.currentItemChanged.connect(lambda *_: self._sync_tools())
        r.queue_changed.connect(self.refresh)
        r.running_changed.connect(lambda _r: self.refresh())
        ctx.project.changed.connect(self._sync_tools)
        self.refresh()

    def _estimate(self, data: dict) -> float | None:
        try:
            from ....case import Case

            return float(self.ctx.services.estimate_runtime(Case.model_validate(data)))
        except Exception:  # noqa: BLE001
            return None

    def add_current(self) -> QueueItem | None:
        data = self.ctx.project.data
        if self.ctx.project.case is None:
            self.ctx.show_message("Le modèle n’est pas valide : corrigez les alertes avant de "
                                  "l’ajouter à la file.")
            return None
        n = sum(1 for i in self.ctx.runner.queue if i.own) + 1
        label = f"{data.get('name', 'cas')} · variante {n}"
        return self.ctx.runner.enqueue(data, label, estimate_s=self._estimate(data))

    def add_file(self, path: str | Path | None = None) -> QueueItem | None:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Ajouter à la file", "",
                                                  "Cas Air-Construct (*.yaml *.yml)")
            if not path:
                return None
        from ...project import Project

        try:
            data = Project.read_file(path)
            item = self.ctx.runner.enqueue(data, f"{data.get('name', Path(path).stem)}",
                                           path=Path(path), estimate_s=self._estimate(data))
        except Exception as exc:  # noqa: BLE001
            self.ctx.show_message(f"Impossible d’ajouter {Path(path).name} : {exc}")
            return None
        return item

    def current_item(self) -> QueueItem | None:
        it = self.list.currentItem()
        if it is None:
            return None
        iid = it.data(0, Qt.ItemDataRole.UserRole)
        return next((i for i in self.ctx.runner.queue if i.id == iid), None)

    def refresh(self) -> None:
        r = self.ctx.runner
        current = self.current_item()
        self.list.clear()
        rows = []
        if r.running and r.current_item is None and r.run_case is not None:
            rows.append((None, f"{r.run_case.name} · modèle actuel", "running"))
        rows += [(i, i.label, i.status) for i in r.queue if i.status == "running"]
        rows += [(i, i.label, i.status) for i in r.queue if i.status == "queued"]
        rows += [(i, i.label, i.status) for i in r.queue
                 if i.status not in ("queued", "running")]
        for item, label, status in rows:
            state = STATUS_TEXT.get(status, status)
            if status == "queued" and item is not None and item.estimate_s:
                state = f"≈ {format_duration(item.estimate_s)}"
            row = QTreeWidgetItem([label, state])
            row.setData(0, Qt.ItemDataRole.UserRole, item.id if item else 0)
            if status in ("running",):
                f = row.font(0)
                f.setBold(True)
                row.setFont(0, f)
            if status == "failed":
                row.setForeground(1, QBrush(QColor(COLORS["error_text"])))
            self.list.addTopLevelItem(row)
            if item is not None and item is current:
                self.list.setCurrentItem(row)
        self.empty.setVisible(not rows)
        self.list.setVisible(bool(rows))
        waiting = r.waiting()
        self.start_button.setVisible(bool(waiting) and not r.running)
        self.start_button.setText("Reprendre la file" if r.queue_paused else "Lancer la file")
        self._sync_tools()

    def rows(self) -> list[tuple[str, str]]:
        return [(self.list.topLevelItem(i).text(0), self.list.topLevelItem(i).text(1))
                for i in range(self.list.topLevelItemCount())]

    def _sync_tools(self) -> None:
        item = self.current_item()
        queued = item is not None and item.status == "queued"
        self.up_button.setEnabled(queued)
        self.down_button.setEnabled(queued)
        self.remove_button.setEnabled(item is not None and item.status != "running")
        self.open_button.setVisible(item is not None and item.outcome is not None
                                    and item.path is not None and not item.own)
        self.add_current_action.setEnabled(self.ctx.project.case is not None)

    def _move(self, delta: int) -> None:
        item = self.current_item()
        if item is not None:
            self.ctx.runner.move_in_queue(item, delta)

    def _remove(self) -> None:
        item = self.current_item()
        if item is not None:
            self.ctx.runner.remove_from_queue(item)

    def open_selected(self) -> bool:
        """Open the project file of an ended item, with its result."""
        item = self.current_item()
        if item is None or item.path is None or item.outcome is None:
            return False
        if self.ctx.runner.running or not self.ctx.open_project(item.path):
            return False
        self.ctx.project.set_results(item.outcome, item.case, item.data)
        self.ctx.goto_tab("results")
        return True

