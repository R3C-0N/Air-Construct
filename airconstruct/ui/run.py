"""Starts, follows, stops and resumes simulation runs; queues the next ones; stores the
outcome in the project.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

:class:`RunController` wraps :class:`~airconstruct.ui.worker.RunWorker` in a QThread.
Any tab may call :meth:`RunController.start` (the Simulation tab's button, the
"Relancer" of stale results, Ctrl+R). Progress is kept on the controller
(``progress_value``, ``progress_max``, ``progress_text``, ``history``, ``phase``) so a
tab built later can show the current state, and is also emitted as signals.

While a run goes on the project is locked (``project.locked``): the model it computes
cannot change under it, which also keeps a stopped run resumable.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QObject, QThread, Signal

from ..case import Case
from .worker import RunOutcome, RunWorker

LOCK_REASON = "Le modèle est verrouillé pendant le calcul : arrêtez-le pour le modifier."

#: solver phases, in order, as logged by runner.run_case ("--- phase <key>: ...") and
#: tagged on monitor.Progress.phase; plus "mesh" before and "post" after the solver
PHASE_KEYS = ("mesh", "flow", "energy", "check", "post")


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


def _default_resume_problem(case: Case, case_dir: Path) -> str | None:
    from ..runner import resume_problem

    return resume_problem(case, case_dir)


def _default_read_history(case_dir: Path) -> list:
    from ..monitor import read_history

    return read_history(case_dir)


@dataclass
class QueueItem:
    """A case waiting in (or gone through) the run queue."""

    label: str
    data: dict  # case data, as Project.data
    path: Path | None = None  # file it was read from (None: a snapshot of the project)
    own: bool = True  # results go to the open project when it ends
    status: str = "queued"  # queued | running | converged | finished | stopped | failed
    outcome: RunOutcome | None = None
    case_dir: Path | None = None
    estimate_s: float | None = None
    id: int = field(default=0)

    @property
    def case(self) -> Case:
        return Case.model_validate(self.data)


def outcome_status(outcome: RunOutcome) -> str:
    if outcome.stopped and outcome.result is not None:
        return "stopped"
    if not outcome.success:
        return "failed"
    return "converged" if getattr(outcome.result, "converged", False) else "finished"


class RunController(QObject):
    started = Signal()
    log = Signal(str)
    stage = Signal(str)  # human readable phase, French
    residual = Signal(int, dict)  # iteration, {field: residual}
    snapshot = Signal(object)  # monitor.Progress of the last iteration (also in history)
    progress = Signal()  # progress_value / progress_max / progress_text changed
    finished = Signal(object)  # RunOutcome (also stored in project.results)
    running_changed = Signal(bool)
    queue_changed = Signal()

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
        #: injectable for tests: why a run directory cannot be resumed (None = it can)
        self.resume_problem_fn: Callable[[Case, Path], str | None] = _default_resume_problem
        self.read_history_fn: Callable[[Path], list] = _default_read_history
        self.thread: QThread | None = None
        self.worker: RunWorker | None = None
        self.run_case: Case | None = None
        self.run_data: dict | None = None
        self.case_dir: Path | None = None  # directory of the current (or last) run
        self.resuming = False
        self.current_item: QueueItem | None = None
        self.queue: list[QueueItem] = []  # waiting, running and ended items
        self.queue_paused = False
        self._next_id = 1
        self._user_stop = False
        self.progress_value = 0
        self.progress_max = 100
        self.progress_text = "Prêt"
        self.last_residuals: dict = {}
        self.log_lines: list[str] = []  # whole log of the current run
        self.history: list = []  # monitor.Progress of the current run, one per iteration
        self.phase = ""  # key of PHASE_KEYS ("" before the start)
        self.started_at: float | None = None  # time.monotonic() of the start
        self.ended_at: float | None = None
        self._start_iteration = 0  # iteration the run (or resume) started from

    @property
    def running(self) -> bool:
        return self.thread is not None

    # ------------------------------------------------------------------ progress
    def _set_progress(self, value: int | None = None, maximum: int | None = None,
                      text: str | None = None) -> None:
        if maximum is not None:
            self.progress_max = maximum
        if value is not None:
            self.progress_value = value
        if text is not None:
            self.progress_text = text
        self.progress.emit()

    @property
    def iteration(self) -> int:
        """Last solver iteration of the current (or last) run."""
        return self.history[-1].iteration if self.history else 0

    def phase_iterations(self) -> dict[str, int]:
        """Iterations done in each solver phase, from the same snapshots as
        :attr:`iteration` (so the counts always add up to it)."""
        out = {k: 0 for k in PHASE_KEYS}
        prev = 0
        for p in self.history:
            key = p.phase if p.phase in out else "flow"
            out[key] += max(0, p.iteration - prev)
            prev = p.iteration
        return out

    def elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.ended_at or time.monotonic()) - self.started_at

    def remaining_s(self) -> float | None:
        """Rough time left from the iteration rate so far (None before it is known)."""
        done = self.iteration - self._start_iteration
        if not self.running or done < 5 or self.progress_max <= 0:
            return None
        rate = self.elapsed_s() / done
        return max(0.0, (self.progress_max - self.iteration) * rate)

    # ------------------------------------------------------------------ start / stop
    def start(self, resume: bool = False) -> bool:
        """Run the current case (or continue the last, stopped run with ``resume``).
        False if it is invalid, cannot be resumed or a run is going on."""
        if self.running:
            return False
        case = self.project.case
        if case is None:
            return False
        case_dir = None
        if resume:
            ok, _why = self.can_resume()
            if not ok:
                return False
            case_dir = Path(self.project.results.outcome.result.case_dir)
        self._launch(case, self.project.data, case_dir, resume, None)
        return True

    def resume(self) -> bool:
        return self.start(resume=True)

    def can_resume(self) -> tuple[bool, str]:
        """(True, "") when the last run of the project can continue where it ended."""
        rec = self.project.results
        if self.running:
            return False, "un calcul est en cours"
        if rec is None or rec.outcome.result is None:
            return False, "aucun calcul à reprendre"
        res, outcome = rec.outcome.result, rec.outcome
        if getattr(res, "converged", False) and not outcome.stopped:
            return False, "le calcul a déjà convergé"
        case = self.project.case
        if case is None:
            return False, "le modèle n’est pas valide"
        try:
            problem = self.resume_problem_fn(case, Path(res.case_dir))
        except Exception as exc:  # noqa: BLE001
            problem = str(exc)
        return (False, problem) if problem else (True, "")

    def _launch(self, case: Case, data: dict, case_dir: Path | None, resume: bool,
                item: QueueItem | None) -> None:
        own = item is None or item.own
        if own:
            self.project.clear_results()
            self.project.set_locked(True, LOCK_REASON)
        self.run_case = case
        self.run_data = copy.deepcopy(data)
        self.case_dir = Path(case_dir) if case_dir else Path(self._work_dir()) / case.name
        self.resuming = resume
        self.current_item = item
        self._user_stop = False
        self.log_lines = []
        self.last_residuals = {}
        self.history = []
        self.phase = ""
        self._start_iteration = 0
        if resume:
            try:
                self.history = list(self.read_history_fn(self.case_dir))
            except Exception:  # noqa: BLE001 - a missing history only empties the chart
                self.history = []
            self._start_iteration = self.iteration
        self.started_at, self.ended_at = time.monotonic(), None
        self._set_progress(self.iteration, max(expected_iterations(case), self.iteration),
                           "Reprise…" if resume else "Démarrage…")
        if item is not None:
            item.status, item.case_dir = "running", self.case_dir

        self.thread = QThread(self)
        self.worker = RunWorker(case, self._work_dir(), self._run_fn, self._summarize_fn,
                                self._load_fn, case_dir=self.case_dir if (resume or item)
                                else None, resume=resume)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._on_log)
        self.worker.residual.connect(self._on_residual)
        self.worker.snapshot.connect(self._on_snapshot)
        self.worker.stage.connect(lambda s: self._on_stage(self.STAGES.get(s, s)))
        self.worker.finished.connect(self._on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_done)
        self.thread.start()
        self.started.emit()
        self.running_changed.emit(True)
        if item is not None:
            self.queue_changed.emit()

    def stop(self) -> None:
        """Stop cleanly: the partial result is kept and the run can be resumed. The
        queue pauses (it does not start the next case)."""
        if self.worker is not None:
            self._user_stop = True
            self.worker.request_stop()
            self._set_progress(text="Arrêt en cours…")

    def wait(self, ms: int = 10000) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(ms)

    # ------------------------------------------------------------------ queue
    def enqueue(self, data: dict, label: str = "", path: Path | None = None,
                own: bool | None = None, estimate_s: float | None = None) -> QueueItem:
        """Add a case to the queue. ``data`` is copied (a snapshot); ``own`` (default:
        no file given) sends its results to the open project when it ends."""
        Case.model_validate(data)  # refuse an invalid case now, not when its turn comes
        item = QueueItem(label or str(data.get("name", "cas")), copy.deepcopy(data),
                         Path(path) if path else None, path is None if own is None else own,
                         estimate_s=estimate_s, id=self._next_id)
        self._next_id += 1
        self.queue.append(item)
        self.queue_changed.emit()
        return item

    def waiting(self) -> list[QueueItem]:
        return [i for i in self.queue if i.status == "queued"]

    def remove_from_queue(self, item: QueueItem) -> None:
        if item.status != "running" and item in self.queue:
            self.queue.remove(item)
            self.queue_changed.emit()

    def move_in_queue(self, item: QueueItem, delta: int) -> None:
        waiting = self.waiting()
        if item not in waiting:
            return
        i = waiting.index(item)
        j = max(0, min(len(waiting) - 1, i + delta))
        if i == j:
            return
        other = waiting[j]
        a, b = self.queue.index(item), self.queue.index(other)
        self.queue[a], self.queue[b] = other, item
        self.queue_changed.emit()

    def clear_ended(self) -> None:
        self.queue = [i for i in self.queue if i.status in ("queued", "running")]
        self.queue_changed.emit()

    def start_queue(self) -> bool:
        """Start the first waiting case (the next ones follow on their own)."""
        self.queue_paused = False
        if self.running:
            return True
        waiting = self.waiting()
        if not waiting:
            return False
        item = waiting[0]
        dirname = f"{item.case.name}-file{item.id}" if not item.own else \
            f"{item.case.name}-q{item.id}"
        self._launch(item.case, item.data, Path(self._work_dir()) / dirname, False, item)
        return True

    # ------------------------------------------------------------------ worker events
    def _on_log(self, line: str) -> None:
        self.log_lines.append(line)
        self.log.emit(line)
        if line.startswith("--- phase "):
            name = line[len("--- phase "):].split(":")[0].strip()
            self.phase = name
            self._on_stage(self.PHASES.get(name, name))
        elif line.startswith("$ "):
            words = line[2:].split()
            cmd = words[0] if words else ""
            if cmd not in ("mpirun",) and not cmd.startswith("chtMultiRegion"):
                self.phase = "mesh"
                self._on_stage(f"Maillage ({cmd})")

    def _on_stage(self, stage: str) -> None:
        if stage == "Post-traitement":
            self.phase = "post"
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
        if not any(not getattr(p, "_synthetic", False) for p in self.history[-3:]):
            # a solver without live monitors (or a test double): build the snapshot
            # from the residuals so the chart and the counts still follow
            from ..monitor import Progress

            p = Progress(iteration, self.phase if self.phase in self.PHASES else "flow",
                         {"": dict(residuals)})
            p._synthetic = True
            self._add_snapshot(p)

    def _on_snapshot(self, p) -> None:
        if not p.phase and self.phase in self.PHASES:
            p.phase = self.phase
        self._add_snapshot(p)

    def _add_snapshot(self, p) -> None:
        if self.history and self.history[-1].iteration == p.iteration:
            if getattr(p, "_synthetic", False) and not getattr(self.history[-1], "_synthetic",
                                                               False):
                return
            self.history[-1] = p
        else:
            self.history.append(p)
        self.snapshot.emit(p)

    def _on_finished(self, outcome: RunOutcome) -> None:
        self.ended_at = time.monotonic()
        res = outcome.result
        if res is not None and getattr(res, "iterations", 0) and \
                res.iterations != self.iteration and self.history and \
                not getattr(self.history[-1], "_synthetic", False):
            # the result counts the solver's last "Time =": keep the two in step
            res.iterations = self.iteration
        self._set_progress(1 if outcome.success else 0, 1, outcome_state(outcome))
        item = self.current_item
        if item is not None:
            item.outcome, item.status = outcome, outcome_status(outcome)
        if (item is None or item.own) and self.run_case is not None \
                and self.run_data is not None:
            self.project.set_locked(False)
            self.project.set_results(outcome, self.run_case, self.run_data)
        self.finished.emit(outcome)
        if item is not None:
            self.queue_changed.emit()

    def _on_thread_done(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None
        self.current_item = None
        self.project.set_locked(False)
        self.running_changed.emit(False)
        if self._user_stop:
            self.queue_paused = bool(self.waiting())
        elif self.waiting() and not self.queue_paused:
            self.start_queue()
        if self.queue_paused:
            self.queue_changed.emit()
