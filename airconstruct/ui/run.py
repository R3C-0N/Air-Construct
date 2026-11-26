"""Starts, follows and stops a simulation run; stores the outcome in the project.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

:class:`RunController` wraps :class:`~airconstruct.ui.worker.RunWorker` in a QThread.
Any tab may call :meth:`RunController.start` (the Simulation tab's button, the
"Relancer" of stale results, Ctrl+R). Progress is kept on the controller
(``progress_value``, ``progress_max``, ``progress_text``) so a tab built later can
show the current state, and is also emitted as signals.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QObject, QThread, Signal

from ..case import Case
from .worker import RunOutcome, RunWorker


def format_duration(seconds: float) -> str:
    """Human readable duration, e.g. '45 s', '12 min', '2 h 05 min'."""
    s = max(0.0, float(seconds))
    if s < 60:
        return f"{s:.0f} s"
    if s < 3600:
        return f"{s / 60:.0f} min"
    h, m = divmod(round(s / 60), 60)
    return f"{h} h {m:02d} min"


def expected_iterations(case: Case) -> int:
    """Iterations a run usually takes (for the progress bar), never above the cap."""
    try:
        from ..estimate import ENERGY_ITERATIONS, FLOW_ITERATIONS

        typical = FLOW_ITERATIONS + ENERGY_ITERATIONS
    except ImportError:
        typical = case.solver.iterations
    return max(1, min(case.solver.iterations, typical))


def outcome_state(outcome: RunOutcome) -> str:
    """One line describing how a run ended, e.g. 'Convergé après 812 itérations en 3 min'."""
    r = outcome.result
    if outcome.stopped:
        state = "Arrêté"
    elif outcome.success:
        state = "Convergé" if getattr(r, "converged", False) else "Terminé (non convergé)"
    else:
        state = "Échoué"
    if r is not None:
        state += f" après {getattr(r, 'iterations', 0)} itérations"
        state += f" en {format_duration(getattr(r, 'wall_time_s', 0.0))}"
    return state


class RunController(QObject):
    started = Signal()
    log = Signal(str)
    stage = Signal(str)  # human readable phase, French
    residual = Signal(int, dict)  # iteration, {field: residual}
    progress = Signal()  # progress_value / progress_max / progress_text changed
    finished = Signal(object)  # RunOutcome (also stored in project.results)
    running_changed = Signal(bool)

    PHASES: ClassVar[dict[str, str]] = {
        "flow": "Calcul de l’écoulement d’air",
        "energy": "Calcul des températures",
        "check": "Vérification de la convection naturelle",
    }
    STAGES: ClassVar[dict[str, str]] = {"Starting": "Démarrage",
                                        "Post-processing": "Post-traitement"}

    def __init__(self, project, work_dir: Callable[[], Path], run_fn: Callable | None = None,
                 summarize_fn: Callable | None = None, load_regions_fn: Callable | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self._work_dir = work_dir
        self._run_fn, self._summarize_fn, self._load_fn = run_fn, summarize_fn, load_regions_fn
        self.thread: QThread | None = None
        self.worker: RunWorker | None = None
        self.run_case: Case | None = None
        self.run_data: dict | None = None
        self.progress_value = 0
        self.progress_max = 100
        self.progress_text = "Prêt"
        self.last_residuals: dict = {}
        self.log_lines: list[str] = []  # whole log of the current run

    @property
    def running(self) -> bool:
        return self.thread is not None

    def _set_progress(self, value: int | None = None, maximum: int | None = None,
                      text: str | None = None) -> None:
        if maximum is not None:
            self.progress_max = maximum
        if value is not None:
            self.progress_value = value
        if text is not None:
            self.progress_text = text
        self.progress.emit()

    def start(self) -> bool:
        """Run the current case. False if it is invalid or a run is going on."""
        if self.running:
            return False
        case = self.project.case
        if case is None:
            return False
        self.project.clear_results()
        self.run_case = case
        self.run_data = self.project.data
        self.log_lines = []
        self.last_residuals = {}
        self._set_progress(0, expected_iterations(case), "Démarrage…")

        self.thread = QThread(self)
        self.worker = RunWorker(case, self._work_dir(), self._run_fn, self._summarize_fn,
                                self._load_fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._on_log)
        self.worker.residual.connect(self._on_residual)
        self.worker.stage.connect(lambda s: self._on_stage(self.STAGES.get(s, s)))
        self.worker.finished.connect(self._on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_done)
        self.thread.start()
        self.started.emit()
        self.running_changed.emit(True)
        return True

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.request_stop()
            self._set_progress(text="Arrêt en cours…")

    def wait(self, ms: int = 10000) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(ms)

    def _on_log(self, line: str) -> None:
        self.log_lines.append(line)
        self.log.emit(line)
        if line.startswith("--- phase "):
            name = line[len("--- phase "):].split(":")[0].strip()
            self._on_stage(self.PHASES.get(name, name))
        elif line.startswith("$ "):
            words = line[2:].split()
            cmd = words[0] if words else ""
            if cmd not in ("mpirun",) and not cmd.startswith("chtMultiRegion"):
                self._on_stage(f"Maillage ({cmd})")

    def _on_stage(self, stage: str) -> None:
        if stage == "Post-traitement":
            self._set_progress(0, 0, "Post-traitement…")  # max 0: busy indicator
        else:
            self._set_progress(text=f"{stage}… itération %v")
        self.stage.emit(stage)

    def _on_residual(self, iteration: int, residuals: dict) -> None:
        cap = self.run_case.solver.iterations if self.run_case else iteration
        maximum = self.progress_max
        if 0 < maximum < iteration:  # longer than usual: show the cap
            maximum = max(iteration, cap)
        value = min(iteration, maximum) if maximum > 0 else self.progress_value
        self.last_residuals = dict(residuals)
        self._set_progress(value, maximum)
        self.residual.emit(iteration, residuals)

    def _on_finished(self, outcome: RunOutcome) -> None:
        self._set_progress(1 if outcome.success else 0, 1, outcome_state(outcome))
        if self.run_case is not None and self.run_data is not None:
            self.project.set_results(outcome, self.run_case, self.run_data)
        self.finished.emit(outcome)

    def _on_thread_done(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None
        self.running_changed.emit(False)
