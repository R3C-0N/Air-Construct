"""Helpers for UI tests: build the main window with mocked solver and results.

Import it before PySide6 widgets are created (the ``qapp`` fixture does).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["AIRCONSTRUCT_NO_3D"] = "1"

from airconstruct.case import Case
from airconstruct.results import ComponentResult, Summary
from airconstruct.runner import RunResult

SHIPPED_EXAMPLE = Path(__file__).parent.parent / "examples" / "electronics_box.yaml"
# frozen geometry (same as test_results): expectations in the tests depend on it
EXAMPLE = Path(__file__).parent / "data" / "results_box.yaml"


def wait_until(qapp, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def fake_run(case, work_dir, backend=None, on_log=None, on_residual=None, should_stop=None):
    on_log("fake solver start")
    for i in range(1, 6):
        on_residual(i, {"Ux": 10.0 ** -i, "h": 0.5 * 10.0 ** -i})
    case_dir = Path(work_dir) / case.name
    case_dir.mkdir(parents=True, exist_ok=True)
    return RunResult(case_dir=case_dir, success=True, converged=True, iterations=5,
                     wall_time_s=1.0)


def fake_summary(case, case_dir):
    return Summary(
        components=[ComponentResult(c.name, c.power, 40.0 + c.power, 30.0)
                    for c in case.components],
        air_t_max_c=35.0, air_v_max=1.5, pressure_drop_pa=2.0, outlet_t_mean_c=28.0,
    )


def make_window(tmp_path, settings, **kw):
    """MainWindow on the frozen example case, throw-away settings, no modal prompts."""
    from airconstruct.ui.app import MainWindow

    kw.setdefault("case", Case.load(EXAMPLE))
    kw.setdefault("work_dir", tmp_path / "runs")
    kw.setdefault("estimate_fn", lambda c: 1.0)
    win = MainWindow(settings=settings, **kw)
    win.confirm_discard = lambda: True
    return win


def run_and_wait(qapp, win, timeout=10.0):
    """Start a run and wait for its outcome (stored in win.project.results)."""
    assert win.start_run()
    assert wait_until(qapp, lambda: not win.runner.running and win.project.results is not None,
                      timeout)
    return win.project.results.outcome
