"""Step 4, Simulation: run, follow, stop and resume the calculation; queue the next ones.

Owned by: thread "Simulation" (see docs/ui-architecture.md).

- right: the "Calcul" card: state, global progress (one iteration count for the
  running and the stopped screens, taken from the live snapshots), phase stepper,
  run / stop / resume buttons, convergence chart (pyqtgraph), solver log;
- left: solver settings, live part temperatures and probes, run queue;
- 3D view: the parts coloured and labelled with their live max temperature, the probes;
- errors: "OpenFOAM est introuvable" (WSL or Linux, with the commands to copy) and
  "Le calcul a échoué" (probable cause from ``airconstruct.diagnose``, end of the log,
  "Aller à la pièce fautive").

While a run goes on the model is locked (``project.locked``, set by the run
controller): the steps Modèle, Air and Volume d'air cannot change it.
"""

from __future__ import annotations

import math
import os
from itertools import pairwise

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ....foam_setup import plan_for, restart_windows
from ...alerts import ERROR
from ...run import PHASE_KEYS, format_duration
from ...theme import COLORS, set_prop
from ...widgets.card import Card, CardColumn
from ..base import StepTab
from .chart import ConvergenceChart
from .dialogs import FailureDialog, OpenFoamDialog
from .errors import failure_report, openfoam_help
from .panels import LiveCard, QueueCard, SolverCard, fmt_c

PHASE_NAMES = {"mesh": "Maillage", "flow": "Écoulement", "energy": "Températures",
               "check": "Flottabilité", "post": "Résultats"}
SCENE_INTERVAL_MS = 1500  # live 3D colours: at most this often
PROBE_INTERVAL_MS = 2000  # probe files read at most this often
LOG_TAIL = 30


def fmt_int(n: int) -> str:
    """2140 -> '2 140' (French thousands separator)."""
    return f"{int(n):,}".replace(",", " ")


def _heat_hex(t: float, t_lo: float, t_hi: float) -> str:
    """Blue (cool) to orange-red (hot) for the live part colours."""
    f = 0.0 if t_hi <= t_lo else max(0.0, min(1.0, (t - t_lo) / (t_hi - t_lo)))
    stops = [(0.0, (0x3B, 0x82, 0xF6)), (0.5, (0xF5, 0x9E, 0x0B)), (1.0, (0xDC, 0x26, 0x26))]
    for (f0, c0), (f1, c1) in pairwise(stops):
        if f <= f1:
            k = (f - f0) / (f1 - f0)
            return "#" + "".join(f"{round(a + (b - a) * k):02x}" for a, b in zip(c0, c1))
    return "#dc2626"


class PhaseStepper(QWidget):
    """Maillage ✓ · Écoulement ✓ 410 it. · Températures 2 140 it. · Flottabilité · Résultats"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        self.labels: dict[str, QLabel] = {}
        for keys in (PHASE_KEYS[:2], PHASE_KEYS[2:]):  # two rows: fits a side panel
            row = QHBoxLayout()
            row.setSpacing(4)
            for key in keys:
                lab = QLabel(PHASE_NAMES[key])
                set_prop(lab, role="pill")
                row.addWidget(lab)
                self.labels[key] = lab
            row.addStretch(1)
            outer.addLayout(row)

    def set_state(self, counts: dict[str, int], done: set[str], current: str | None,
                  tone: str = "info") -> None:
        for key, lab in self.labels.items():
            text = PHASE_NAMES[key]
            if key in done:
                text += " ✓"
            n = counts.get(key, 0)
            if n and key not in ("mesh", "post"):
                text += f" {fmt_int(n)} it."
            lab.setText(text)
            set_prop(lab, tone=tone if key == current else "success" if key in done else "")

    def texts(self) -> list[str]:
        return [self.labels[k].text() for k in PHASE_KEYS]


class RunCard(Card):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Calcul", parent)
        self.state_pill = QLabel("Prêt")
        set_prop(self.state_pill, role="pill")
        self.header_layout.addWidget(self.state_pill)
        self.headline = QLabel("")
        set_prop(self.headline, role="title")
        self.headline.setWordWrap(True)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        set_prop(self.detail, role="muted")
        self.time_label = QLabel("")
        set_prop(self.time_label, role="muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("Prêt")

        self.run_button = QPushButton("Lancer le calcul")
        self.run_button.setDefault(True)
        set_prop(self.run_button, kind="primary")
        self.stop_button = QPushButton("Arrêter et garder le résultat partiel")
        self.resume_button = QPushButton("Reprendre")
        set_prop(self.resume_button, kind="primary")
        self.restart_button = QPushButton("Relancer depuis le début")
        self.results_button = QPushButton("Voir le résultat partiel")
        set_prop(self.results_button, kind="flat")
        self.details_button = QPushButton("Voir le détail")
        set_prop(self.details_button, kind="flat")
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for b in (self.run_button, self.stop_button, self.resume_button, self.restart_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        links = QHBoxLayout()
        links.addWidget(self.results_button)
        links.addWidget(self.details_button)
        links.addStretch(1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.blocked_label = QLabel("")
        self.blocked_label.setWordWrap(True)
        self.blocked_label.setStyleSheet(f"color: {COLORS['error_text']};")

        self.stepper = PhaseStepper()
        self.residual_label = QLabel("")
        self.residual_label.setWordWrap(True)
        set_prop(self.residual_label, role="muted")
        self.log_button = QToolButton()
        self.log_button.setText("journal complet")
        self.log_button.setCheckable(True)
        res_row = QHBoxLayout()
        res_row.addWidget(self.residual_label, 1)
        res_row.addWidget(self.log_button)

        chart_title = QLabel("CONVERGENCE (LOG)")
        set_prop(chart_title, role="section")
        self.chart = ConvergenceChart()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText("Le journal du solveur s’affiche ici.")
        self.log.setMinimumHeight(160)
        self.log.setVisible(False)
        self.log_button.toggled.connect(self.log.setVisible)

        b = self.body
        b.addWidget(self.headline)
        b.addWidget(self.detail)
        b.addWidget(self.progress)
        b.addWidget(self.time_label)
        b.addLayout(buttons)
        b.addLayout(links)
        b.addWidget(self.note)
        b.addWidget(self.blocked_label)
        b.addWidget(self.stepper)
        b.addLayout(res_row)
        b.addWidget(chart_title)
        b.addWidget(self.chart, 2)
        b.addWidget(self.log, 1)


class SimulationTab(StepTab):
    key = "simulation"
    title = "Simulation"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        r = ctx.runner
        self.card = RunCard()
        # the names the shell tests and other tabs use
        self.run_button, self.stop_button = self.card.run_button, self.card.stop_button
        self.resume_button, self.restart_button = self.card.resume_button, self.card.restart_button
        self.progress, self.log = self.card.progress, self.card.log
        self.residual_label, self.blocked_label = self.card.residual_label, self.card.blocked_label
        self.chart, self.stepper = self.card.chart, self.card.stepper

        self.solver_card = SolverCard(ctx)
        self.live_card = LiveCard(ctx)
        self.queue_card = QueueCard(ctx)
        self._left = CardColumn(self.solver_card, self.live_card, self.queue_card)
        self._right = self.card

        # tools above the 3D view
        self._toolbar = QWidget()
        tl = QHBoxLayout(self._toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        self.probe_tool = QToolButton()
        self.probe_tool.setText("Poser une sonde")
        self.probe_tool.setToolTip("Poser une sonde au centre de la sélection (P)")
        self.labels_tool = QToolButton()
        self.labels_tool.setText("Températures sur les pièces")
        self.labels_tool.setCheckable(True)
        self.labels_tool.setChecked(bool(ctx.settings.value("simulation/labels", True,
                                                            type=bool)))
        set_prop(self.labels_tool, kind="segment")
        tl.addWidget(self.probe_tool)
        tl.addWidget(self.labels_tool)
        self.probe_action = QAction("Poser une sonde", self)
        self.probe_action.setShortcut(QKeySequence("P"))
        self.probe_action.triggered.connect(self.live_card.add_probe)
        self.probe_tool.clicked.connect(self.live_card.add_probe)
        self.labels_tool.toggled.connect(self._on_labels_toggled)

        self.failure_dialog: FailureDialog | None = None
        self.openfoam_dialog: OpenFoamDialog | None = None
        #: injectable for tests: is OpenFOAM reachable now? -> (ok, message)
        self.check_backend = _default_check_backend
        #: injectable for tests: builds the foam_setup.FoamSetup of "Installer
        #: automatiquement" (None: the real one), and restarts Windows
        self.setup_factory = None
        self.restart_windows = restart_windows
        bashrc = ctx.settings.value("simulation/foam_bashrc", "", type=str)
        if bashrc and not os.environ.get("AIRCONSTRUCT_FOAM_BASHRC"):
            os.environ["AIRCONSTRUCT_FOAM_BASHRC"] = bashrc

        self._scene_timer = QTimer(self.card)  # dies with the window
        self._scene_timer.setSingleShot(True)
        self._scene_timer.setInterval(SCENE_INTERVAL_MS)
        self._scene_timer.timeout.connect(self.scene_changed.emit)
        self._probe_timer = QTimer(self.card)  # dies with the window
        self._probe_timer.setInterval(PROBE_INTERVAL_MS)
        self._probe_timer.timeout.connect(self.read_probes)
        self._clock = QTimer(self.card)  # dies with the window
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._refresh)
        self._chart_timer = QTimer(self.card)  # dies with the window
        self._chart_timer.setSingleShot(True)
        self._chart_timer.setInterval(250)
        self._chart_timer.timeout.connect(lambda: self.chart.set_history(r.history))
        self.live_temperatures: dict[str, float] = {}

        c = self.card
        c.run_button.clicked.connect(self.start)
        c.stop_button.clicked.connect(r.stop)
        c.resume_button.clicked.connect(self.resume)
        c.restart_button.clicked.connect(self.start)
        c.results_button.clicked.connect(lambda: ctx.goto_tab("results"))
        c.details_button.clicked.connect(self.show_error_details)

        r.started.connect(self._on_started)
        r.log.connect(self.log.appendPlainText)
        r.progress.connect(self._on_progress)
        r.snapshot.connect(self._on_snapshot)
        r.finished.connect(self._on_finished)
        r.running_changed.connect(lambda _running: self._refresh())
        ctx.alerts.changed.connect(self._refresh)
        ctx.project.results_changed.connect(self._refresh)
        ctx.project.changed.connect(self._on_project_changed)
        ctx.selection.changed.connect(lambda _k: self.active and self.scene_changed.emit())
        self._refresh()

    # ---- StepTab
    def left_panel(self) -> QWidget:
        return self._left

    def right_panel(self) -> QWidget:
        return self._right

    def viewport_toolbar(self) -> QWidget:
        return self._toolbar

    def actions(self) -> list[QAction]:
        return [self.probe_action]

    def activated(self) -> None:
        self._refresh()

    def closing(self) -> None:
        for t in (self._scene_timer, self._probe_timer, self._clock, self._chart_timer):
            t.stop()
        if self.openfoam_dialog is not None:
            self.openfoam_dialog.shutdown()
        for dlg in (self.failure_dialog, self.openfoam_dialog):
            if dlg is not None:
                dlg.close()
        self.chart.dispose()

    def offer_setup(self) -> OpenFoamDialog | None:
        """``--setup-openfoam`` (end of the Windows installer, or after the restart that
        WSL2 needed): open the installation dialog if OpenFOAM is still missing."""
        try:
            ok, msg = self.check_backend()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)
        if ok:
            return None
        return self.show_openfoam_dialog(msg)

    def scene(self):
        case = self.ctx.project.last_valid_case
        if case is None:
            return None
        try:
            from ...view import geometry_items

            items = geometry_items(case)
        except ImportError:
            return None
        temps = self.live_temperatures
        names = {c.name for c in case.components}
        if temps:
            shown = [t for r, t in temps.items() if r in names and math.isfinite(t)]
            lo = min([case.ambient_temperature] + shown)
            hi = max([lo + 1.0] + shown)
            for it in items:
                t = temps.get(it.name)
                if it.name in names and t is not None and math.isfinite(t):
                    it.color = _heat_hex(t, lo, hi)
                    if self.labels_tool.isChecked():
                        it.label = f"{it.name} · {fmt_c(t)}"
        items += self.probe_items(case)
        return items

    def probe_items(self, case) -> list:
        import pyvista as pv

        from ...view import SceneItem

        E = case.enclosure.as_tuple()
        radius = 0.012 * (E[0] ** 2 + E[1] ** 2 + E[2] ** 2) ** 0.5
        out = []
        for p in case.probes:
            label = None
            if self.labels_tool.isChecked():
                v = self.live_card.probe_text(p.name)
                label = p.name if v == "—" else f"{p.name} · {v}"
            out.append(SceneItem(f"probe_{p.name}", pv.Sphere(radius=radius, center=p.point.as_tuple()),
                                 COLORS["accent"], key=f"probe:{p.name}", label=label))
        return out

    # ---- actions
    def start(self) -> bool:
        if self.ctx.runner.running:
            return False
        return self.ctx.runner.start()

    def resume(self) -> bool:
        ok, why = self.ctx.runner.can_resume()
        if not ok:
            self.ctx.show_message(f"Impossible de reprendre : {why}")
            return False
        return self.ctx.runner.resume()

    def _on_labels_toggled(self, on: bool) -> None:
        self.ctx.settings.setValue("simulation/labels", bool(on))
        self.scene_changed.emit()

    # ---- run events
    def _on_started(self) -> None:
        r = self.ctx.runner
        self.log.clear()
        self.log.appendPlainText("\n".join(r.log_lines))
        self.residual_label.setText("")
        self.live_temperatures = {}
        self.live_card.set_temperatures({}, "Températures dès les premières itérations…")
        self.live_card.set_probe_values({})
        self.chart.set_history(r.history)
        for dlg in (self.failure_dialog, self.openfoam_dialog):
            if dlg is not None:
                dlg.close()
        self._clock.start()
        if r.run_case is not None and r.run_case.probes:
            self._probe_timer.start()
        self.ctx.goto_tab(self.key)
        self._refresh()

    def _on_progress(self) -> None:
        r = self.ctx.runner
        self.progress.setRange(0, r.progress_max)
        if r.progress_max > 0:
            self.progress.setValue(min(r.progress_value, r.progress_max))
        self.progress.setFormat(r.progress_text)
        self._refresh_progress_text()

    def _on_snapshot(self, p) -> None:
        worst_f, worst = "", 0.0
        for regs in p.residuals.values():
            for f, v in regs.items():
                if v is not None and math.isfinite(v) and v > worst:
                    worst_f, worst = f, v
        if worst_f:
            self.residual_label.setText(f"Itération {fmt_int(p.iteration)} · résidu max "
                                        f"{worst_f} : {worst:.1e}")
        temps = {reg: t.get("max_c", math.nan) for reg, t in p.temperatures.items()}
        if temps:
            self.live_temperatures = temps
            self.live_card.set_temperatures(temps)
            if self.active and not self._scene_timer.isActive():
                self._scene_timer.start()
        if not self._chart_timer.isActive():
            self._chart_timer.start()
        self._refresh_progress_text()

    def _on_finished(self, outcome) -> None:
        r = self.ctx.runner
        self._clock.stop()
        self._probe_timer.stop()
        self._chart_timer.stop()
        self.chart.set_history(r.history)
        res = outcome.result
        if not self.live_temperatures and res is not None:
            temps = {reg: t[1] for reg, t in (getattr(res, "region_temperatures", {}) or {}).items()}
            if temps:
                self.live_temperatures = temps
                self.live_card.set_temperatures(temps)
        if not self.live_temperatures:
            self.live_card.set_temperatures({}, "Pas de température relevée pendant ce calcul.")
        self.read_probes()
        self.scene_changed.emit()
        self._refresh()
        if outcome.backend_missing is not None:
            self.show_openfoam_dialog(outcome.backend_missing)
        elif not outcome.success:
            self.show_failure_dialog(outcome)

    def read_probes(self) -> None:
        r = self.ctx.runner
        case, case_dir = r.run_case, r.case_dir
        if case is None or case_dir is None or not case.probes:
            return
        try:
            from ....probes import read_probes

            series = read_probes(case, case_dir)
        except Exception:  # noqa: BLE001 - files being written or not there yet
            return
        self.live_card.set_probe_values({n: s.last() for n, s in series.items()})
        if self.active and not self._scene_timer.isActive():
            self._scene_timer.start()

    def _on_project_changed(self) -> None:
        self._refresh()
        if self.active:
            self.scene_changed.emit()

    # ---- errors
    def show_openfoam_dialog(self, message: str) -> OpenFoamDialog:
        old = self.openfoam_dialog
        if old is not None and old.running():  # an installation is going on: show it
            old.open()
            return old
        if old is not None:
            old.close()
        dlg = OpenFoamDialog(openfoam_help(message), lambda: self.check_backend(), self._set_bashrc,
                             self.card.window(), plan=plan_for(message),
                             setup_factory=self.setup_factory, restart=self.restart_windows)
        dlg.accepted.connect(self._refresh)
        self.openfoam_dialog = dlg
        dlg.open()
        return dlg

    def _set_bashrc(self, path: str) -> None:
        os.environ["AIRCONSTRUCT_FOAM_BASHRC"] = path
        self.ctx.settings.setValue("simulation/foam_bashrc", path)

    def show_failure_dialog(self, outcome) -> FailureDialog:
        r = self.ctx.runner
        case = r.run_case or self.ctx.project.last_valid_case
        names = {c.name for c in case.components} if case is not None else None
        wall = getattr(outcome.result, "wall_time_s", None)
        report = failure_report(outcome.diagnosis, outcome.errors,
                                format_duration(wall) if wall else "", names)
        if self.failure_dialog is not None:
            self.failure_dialog.close()
        dlg = FailureDialog(report, r.log_lines[-LOG_TAIL:], self.goto_part, self.card.window())
        self.failure_dialog = dlg
        dlg.open()
        return dlg

    def show_error_details(self) -> None:
        rec = self.ctx.project.results
        if rec is None:
            return
        if rec.outcome.backend_missing is not None:
            self.show_openfoam_dialog(rec.outcome.backend_missing)
        else:
            self.show_failure_dialog(rec.outcome)

    def goto_part(self, part: str) -> None:
        self.ctx.goto_tab("model")
        self.ctx.selection.set([f"component:{part}"])

    # ---- state
    def _refresh(self) -> None:
        r, p, c = self.ctx.runner, self.ctx.project, self.card
        running = r.running
        errors = [a for a in self.ctx.alerts.alerts if a.level == ERROR]
        rec = p.results
        outcome = rec.outcome if rec is not None else None
        can_run = not running and p.case is not None and not errors
        can_resume = (not running and outcome is not None and outcome.result is not None
                      and (outcome.stopped or (outcome.success
                                               and not getattr(outcome.result, "converged",
                                                               False)))
                      and r.can_resume()[0] and not errors)
        c.run_button.setEnabled(can_run)
        c.restart_button.setEnabled(can_run)
        c.resume_button.setEnabled(bool(can_resume))
        c.stop_button.setEnabled(running)
        c.blocked_label.setText("" if not errors else
                                f"Le cas n’est pas valide : {errors[0].message}")

        stopped = outcome is not None and outcome.stopped and outcome.result is not None
        failed = outcome is not None and not outcome.success and not stopped
        partial = stopped or (outcome is not None and outcome.success
                              and not getattr(outcome.result, "converged", False))
        c.run_button.setVisible(not running and not stopped)
        c.stop_button.setVisible(running)
        c.resume_button.setVisible(not running and stopped or (not running and partial))
        c.restart_button.setVisible(not running and stopped)
        c.results_button.setVisible(not running and outcome is not None and outcome.success)
        c.results_button.setText("Voir le résultat partiel" if partial else "Voir les résultats")
        c.details_button.setVisible(not running and failed)
        if not running and not stopped and outcome is not None and outcome.success:
            c.run_button.setText("Relancer le calcul")
        else:
            c.run_button.setText("Lancer le calcul")

        if running:
            tone, pill = "info", "En cours" if not r.resuming else "Reprise"
        elif outcome is None:
            tone, pill = "", "Prêt"
        elif outcome.backend_missing is not None:
            tone, pill = "error", "OpenFOAM absent"
        elif stopped:
            tone, pill = "warning", "Arrêté"
        elif failed:
            tone, pill = "error", "Échoué"
        elif partial:
            tone, pill = "warning", "Non convergé"
        else:
            tone, pill = "success", "Convergé"
        c.state_pill.setText(pill)
        set_prop(c.state_pill, tone=tone)
        self._refresh_progress_text()

    def _refresh_progress_text(self) -> None:
        self._fill_progress_text()
        c = self.card
        for lab in (c.note, c.detail, c.time_label, c.blocked_label):
            lab.setVisible(bool(lab.text()))

    def _fill_progress_text(self) -> None:
        r, c = self.ctx.runner, self.card
        rec = self.ctx.project.results
        outcome = rec.outcome if rec is not None else None
        counts = r.phase_iterations()
        it = r.iteration
        case = r.run_case or self.ctx.project.case
        cap = case.solver.iterations if case is not None else 0
        phase = r.phase if r.phase in PHASE_NAMES else ""
        last_solver = next((p.phase for p in reversed(r.history) if p.phase), "")
        if r.running:
            expected = max(1, r.progress_max)
            pct = min(99, int(100 * it / expected)) if phase not in ("", "mesh") else 0
            if phase == "post":
                pct = 99
            c.headline.setText(f"Progression globale {pct} %")
            parts = []
            if phase:
                parts.append(f"phase {PHASE_NAMES[phase]}")
            if it:
                parts.append(f"itération {fmt_int(it)} (≈ {fmt_int(expected)} attendues, "
                             f"limite {fmt_int(cap)})")
            c.detail.setText(" · ".join(parts) or "Démarrage…")
            left = r.remaining_s()
            c.time_label.setText(f"écoulé {format_duration(r.elapsed_s())}"
                                 + (f" · ≈ {format_duration(left)} restantes"
                                    if left is not None else ""))
            order = list(PHASE_KEYS)
            cur = phase or "mesh"
            done = set(order[:order.index(cur)]) if cur in order else set()
            if r.resuming:
                done.add("mesh")
            c.stepper.set_state(counts, done, cur)
            c.note.setText("")
            c.stepper.setVisible(True)
            return
        c.time_label.setText("")
        if outcome is None:
            c.headline.setText("Prêt à lancer")
            est = None
            if self.ctx.project.case is not None:
                try:
                    est = self.ctx.services.estimate_runtime(self.ctx.project.case)
                except Exception:  # noqa: BLE001
                    est = None
            c.detail.setText(f"Durée estimée ≈ {format_duration(est)}" if est else "")
            c.note.setText("")
            c.stepper.set_state({}, set(), None)
            return
        res = outcome.result
        n = it if r.history else getattr(res, "iterations", 0)
        phase_txt = PHASE_NAMES.get(last_solver, "")
        if outcome.backend_missing is not None:
            c.headline.setText("OpenFOAM est introuvable")
            c.detail.setText(outcome.backend_missing.splitlines()[0])
            c.note.setText("")
            c.stepper.set_state({}, set(), None)
            return
        if outcome.stopped and res is not None:
            c.headline.setText(f"Arrêté par vous après {fmt_int(n)} itérations"
                               + (f" (phase {phase_txt})" if phase_txt else ""))
            c.detail.setText(f"limite {fmt_int(cap)} itérations")
            c.note.setText("Le résultat partiel est enregistré mais <b>non convergé</b> : les "
                           "températures peuvent encore évoluer de quelques degrés.")
            done = {k for k in ("mesh", "flow", "energy", "check") if k == "mesh"
                    or (counts.get(k) and k != last_solver)}
            c.stepper.set_state(counts, done, last_solver or None, "warning")
            return
        if not outcome.success:
            report_line = outcome.errors[0].splitlines()[0] if outcome.errors else ""
            diag = outcome.diagnosis
            cause = getattr(diag[0], "message", "") if diag else ""
            c.headline.setText("Le calcul a échoué")
            c.detail.setText(cause or report_line)
            c.note.setText("")
            c.stepper.set_state(counts, {"mesh"} if n else set(),
                                (last_solver or "mesh"), "error")
            return
        converged = getattr(res, "converged", False)
        c.headline.setText(("Convergé" if converged else "Terminé sans converger")
                           + f" après {fmt_int(n)} itérations")
        c.detail.setText(f"en {format_duration(getattr(res, 'wall_time_s', 0.0))}")
        diag = outcome.diagnosis
        c.note.setText("" if converged else
                       (getattr(diag[0], "message", "") if diag else
                        "La limite d’itérations est atteinte : reprenez le calcul pour "
                        "l’affiner."))
        c.stepper.set_state(counts, set(PHASE_KEYS), None)


def _default_check_backend() -> tuple[bool, str]:
    from ....runner import detect_backend

    return detect_backend().available()

