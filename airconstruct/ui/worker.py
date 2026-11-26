"""Background simulation run, so the window stays responsive.

Owned by: workstream "ui".

:class:`RunWorker` lives in a QThread. It calls runner.run_case, then
results.summarize and results.load_regions, and reports through Qt signals
(delivered in the GUI thread). The functions are injectable for tests.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ..case import Case


@dataclass
class RunOutcome:
    """Everything the window needs once the run is over."""

    result: object | None = None  # runner.RunResult
    summary: object | None = None  # results.Summary
    regions: dict = field(default_factory=dict)  # {region: pyvista grid}
    errors: list[str] = field(default_factory=list)
    stopped: bool = False

    @property
    def success(self) -> bool:
        return bool(self.result is not None and getattr(self.result, "success", False))


def _default_run(*args, **kwargs):
    from ..runner import run_case

    return run_case(*args, **kwargs)


def _default_summarize(case: Case, case_dir: Path):
    from ..results import summarize

    return summarize(case, case_dir)


def _default_load_regions(case_dir: Path) -> dict:
    from ..results import load_regions

    return load_regions(case_dir)


class RunWorker(QObject):
    log = Signal(str)
    residual = Signal(int, dict)
    stage = Signal(str)
    finished = Signal(object)  # RunOutcome

    def __init__(
        self,
        case: Case,
        work_dir: Path,
        run_fn: Callable | None = None,
        summarize_fn: Callable | None = None,
        load_regions_fn: Callable | None = None,
    ) -> None:
        super().__init__()
        self.case = case
        self.work_dir = Path(work_dir)
        self.run_fn = run_fn or _default_run
        self.summarize_fn = summarize_fn or _default_summarize
        self.load_regions_fn = load_regions_fn or _default_load_regions
        self._stop = False

    def request_stop(self) -> None:
        """Thread safe: the runner polls should_stop()."""
        self._stop = True

    def _error(self, out: RunOutcome, what: str, exc: BaseException) -> None:
        if isinstance(exc, NotImplementedError):
            msg = f"{what} is not implemented yet"
        else:
            msg = f"{what} failed: {exc}"
        out.errors.append(msg)
        self.log.emit(msg)
        self.log.emit("".join(traceback.format_exception(exc)).rstrip())

    @Slot()
    def run(self) -> None:
        out = RunOutcome()
        try:
            self.stage.emit("Solving")
            self.work_dir.mkdir(parents=True, exist_ok=True)
            out.result = self.run_fn(
                self.case,
                self.work_dir,
                on_log=self.log.emit,
                on_residual=lambda it, res: self.residual.emit(int(it), dict(res)),
                should_stop=lambda: self._stop,
            )
            out.stopped = self._stop
            out.errors.extend(getattr(out.result, "errors", []) or [])
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self._error(out, "Simulation", exc)
        if out.result is not None and getattr(out.result, "success", False):
            case_dir = Path(out.result.case_dir)
            self.stage.emit("Post-processing")
            try:
                out.summary = self.summarize_fn(self.case, case_dir)
            except Exception as exc:  # noqa: BLE001
                self._error(out, "Result summary", exc)
            try:
                out.regions = self.load_regions_fn(case_dir) or {}
            except Exception as exc:  # noqa: BLE001
                self._error(out, "Loading result fields", exc)
        self.finished.emit(out)
