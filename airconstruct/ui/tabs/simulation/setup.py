"""Background runner of ``foam_setup.FoamSetup`` for the "OpenFOAM est introuvable" dialog.

Owned by: thread "Simulation".

:class:`SetupWorker` lives in a QThread and reports through Qt signals (delivered in
the GUI thread). :func:`default_setup` builds the real ``FoamSetup``; tests pass their
own factory with a fake shell.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ....foam_setup import FoamSetup, SetupPlan, SetupResult

#: (plan, check, on_step, on_line, on_progress) -> FoamSetup
SetupFactory = Callable[..., FoamSetup]


def default_setup(plan: SetupPlan, check: Callable[[], tuple[bool, str]], on_step, on_line,
                  on_progress) -> FoamSetup:
    return FoamSetup(plan, check, on_step=on_step, on_line=on_line, on_progress=on_progress)


class SetupWorker(QObject):
    step = Signal(str, str)  # step key, "running" | "done" | "skipped"
    line = Signal(str)
    progress = Signal(float)  # 0..1 within the OpenFOAM step
    finished = Signal(object)  # foam_setup.SetupResult

    def __init__(self, plan: SetupPlan, check: Callable[[], tuple[bool, str]],
                 factory: SetupFactory = default_setup) -> None:
        super().__init__()
        self.setup = factory(plan, check, self.step.emit, self.line.emit, self.progress.emit)

    @Slot()
    def run(self) -> None:
        try:
            result = self.setup.run()
        except Exception as exc:  # noqa: BLE001 - shown to the user, never lost
            result = SetupResult("failed", f"Erreur inattendue : {exc}")
        self.finished.emit(result)

    def cancel(self) -> None:
        self.setup.cancel()


def start(worker: SetupWorker) -> QThread:
    """Run ``worker`` in a new thread; the thread quits when the worker finishes."""
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    thread.start()
    return thread
