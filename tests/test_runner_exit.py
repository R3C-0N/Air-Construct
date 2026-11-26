"""run_case with a fake backend: a crash after the solver printed End is not a failure."""

from pathlib import Path

from airconstruct.case import Case
from airconstruct.runner import Backend, run_case

EXAMPLE = Path(__file__).parent.parent / "examples" / "electronics_box.yaml"


class CrashAfterEnd(Backend):
    """Meshing tools succeed silently; the solver runs to endTime, prints End, then dies
    with SIGSEGV (what OpenFOAM does when stopAtWriteNow arrives during teardown)."""

    def __init__(self, exit_code: int = -11):
        self.exit_code = exit_code
        self.solver_runs = 0

    def available(self):
        return True, "fake"

    def write_now(self):
        pass

    def run(self, argv, cwd, on_line=None):
        if "chtMultiRegionSimpleFoam" not in argv:
            return 0
        self.solver_runs += 1
        for it in range(1, 4):
            on_line(f"Time = {it}")
            on_line("Solving for fluid region air")
            on_line("DILUPBiCGStab:  Solving for Ux, Initial residual = 1e-06, "
                    "Final residual = 1e-09, No Iterations 1")
            on_line("ExecutionTime = 0.1 s  ClockTime = 0 s")
        on_line("End")
        on_line("sigStopAtWriteNow : setting up write and stop at end of the next iteration")
        return self.exit_code


def test_crash_after_end_is_success(tmp_path):
    case = Case.load(EXAMPLE)
    case.solver.iterations = 3
    res = run_case(case, tmp_path, CrashAfterEnd())
    assert res.success, res.errors
    assert res.errors == []


def test_crash_without_end_is_failure(tmp_path):
    class Crash(CrashAfterEnd):
        def run(self, argv, cwd, on_line=None):
            if "chtMultiRegionSimpleFoam" not in argv:
                return 0
            on_line("Time = 1")
            return -11

    case = Case.load(EXAMPLE)
    res = run_case(case, tmp_path, Crash())
    assert not res.success
    assert "exit -11" in res.errors[0]
