"""run_case phase control with a fake backend (no OpenFOAM needed)."""

import re
from pathlib import Path

from test_case import EXAMPLE

from airconstruct.case import Case
from airconstruct.runner import END_TIME_MARGIN, Backend, run_case

ITERATION = """Time = {i}

Solving for fluid region air
DILUPBiCGStab:  Solving for Ux, Initial residual = 1e-06, Final residual = 1e-08, No Iterations 1
GAMG:  Solving for p_rgh, Initial residual = 1e-06, Final residual = 1e-08, No Iterations 1
Min/max T:298.15 310

Solving for solid region cpu
DICPCG:  Solving for h, Initial residual = 0.3, Final residual = 1e-11, No Iterations 8
Min/max T:320 330
ExecutionTime = 0.1 s  ClockTime = 0 s
"""


class FakeSolver(Backend):
    """Every command succeeds; the solver iterates until write_now, then prints End."""

    def __init__(self, ignore_signal: bool = False):
        self.end_times: list[int] = []
        self.signals = 0
        self.stops = 0
        self.ignore_signal = ignore_signal
        self.it = 0

    def available(self):
        return True, "fake"

    def write_now(self):
        self.signals += 1
        if not self.ignore_signal:
            self._stop_next = True

    def stop(self):
        self.stops += 1
        self._stop_next = True

    def run(self, argv, cwd, on_line=None):
        if "chtMultiRegionSimpleFoam" not in argv:
            return 0
        txt = (Path(cwd) / "system/controlDict").read_text()
        end = int(re.search(r"endTime\s+(\d+);", txt).group(1))
        self.end_times.append(end)
        self._stop_next = False
        while self.it < end and not self._stop_next:
            self.it += 1
            for line in ITERATION.format(i=self.it).splitlines():
                on_line(line)
        on_line("End")
        return 0


def test_phases_end_by_signal_never_by_end_time(tmp_path):
    case = Case.load(EXAMPLE)
    case.solver.iterations = 400
    backend = FakeSolver()
    res = run_case(case, tmp_path, backend)
    assert res.success
    # each solver launch was asked to stop exactly once, well before its endTime
    assert backend.signals == len(backend.end_times) >= 2
    assert all(e > 400 + END_TIME_MARGIN // 2 for e in backend.end_times)
    assert backend.it <= 400 + len(backend.end_times)
    assert backend.stops == 0


def test_lost_signal_falls_back_to_stop(tmp_path):
    case = Case.load(EXAMPLE)
    case.solver.iterations = 300
    backend = FakeSolver(ignore_signal=True)
    res = run_case(case, tmp_path, backend)
    assert res.success
    assert backend.stops >= 1
    assert backend.signals == backend.stops  # one signal per launch, never repeated
