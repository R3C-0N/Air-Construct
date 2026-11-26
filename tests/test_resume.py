"""Run state (run.json), stop and resume, with a fake backend (no OpenFOAM needed)."""

import re
from pathlib import Path

from test_case import EXAMPLE
from test_runner_phases import ITERATION

from airconstruct.case import Case
from airconstruct.monitor import read_history
from airconstruct.runner import (
    Backend,
    latest_iteration,
    read_state,
    resume_case,
    resume_problem,
    run_case,
)


class DiskSolver(Backend):
    """Meshing creates the region mesh directories; the solver starts from the latest
    time directory (like startFrom latestTime), iterates until write_now and writes
    its last iteration."""

    def __init__(self):
        self.commands: list[str] = []

    def available(self):
        return True, "fake"

    def write_now(self):
        self._stop = True

    def stop(self):
        self._stop = True

    def run(self, argv, cwd, on_line=None):
        cwd = Path(cwd)
        self.commands.append(argv[0])
        if argv[0] == "splitMeshRegions":
            for region in ["air", "cpu"]:
                (cwd / "constant" / region / "polyMesh").mkdir(parents=True, exist_ok=True)
        if "chtMultiRegionSimpleFoam" not in argv:
            return 0
        end = int(re.search(r"endTime\s+(\d+);", (cwd / "system/controlDict").read_text())[1])
        it = latest_iteration(cwd)
        self._stop = False
        while it < end and not self._stop:
            it += 1
            for line in ITERATION.format(i=it).splitlines():
                on_line(line)
        (cwd / str(it) / "air").mkdir(parents=True, exist_ok=True)
        (cwd / str(it) / "air" / "T").write_text("T")
        on_line("End")
        return 0


def _case(iterations=400):
    case = Case.load(EXAMPLE)
    case.solver.iterations = iterations
    return case


def test_state_of_a_finished_run(tmp_path):
    res = run_case(_case(300), tmp_path, DiskSolver())
    assert res.success and not res.stopped
    st = read_state(res.case_dir)
    assert st["status"] in ("converged", "finished")
    assert st["iteration"] == res.iterations and st["resumes"] == 0
    assert st["started_at"] <= st["updated_at"]


def test_stop_then_resume(tmp_path):
    backend = DiskSolver()
    calls = {"n": 0}

    def stop_at_120():
        calls["n"] += 1
        return calls["n"] >= 120

    res = run_case(_case(), tmp_path, backend, should_stop=stop_at_120)
    assert res.success and res.stopped and not res.converged
    assert read_state(res.case_dir)["status"] == "stopped"
    stopped_at = latest_iteration(res.case_dir)
    assert 100 <= stopped_at <= 125
    assert resume_problem(_case(), res.case_dir) is None
    n_snapshots = len(read_history(res.case_dir))

    backend2 = DiskSolver()
    res2 = resume_case(res.case_dir, backend2)
    assert res2.success, res2.errors
    assert res2.case_dir == res.case_dir
    # no new mesh, no new initial fields: straight to the solver, from where it stopped
    assert set(backend2.commands) == {"chtMultiRegionSimpleFoam"}
    assert res2.iterations > stopped_at
    st = read_state(res.case_dir)
    assert st["resumes"] == 1 and st["status"] in ("converged", "finished")
    hist = read_history(res.case_dir)
    assert len(hist) > n_snapshots  # the history goes on in the same file
    assert hist[n_snapshots].iteration == stopped_at + 1
    assert "--- resuming" not in (res.case_dir / "log.blockMesh").read_text()


def test_resume_with_more_iterations(tmp_path):
    res = run_case(_case(150), tmp_path, DiskSolver())
    assert res.iterations <= 151
    more = _case(400)
    res2 = resume_case(res.case_dir, DiskSolver(), case=more)
    assert res2.success and res2.iterations > 151


def test_resume_refuses_a_changed_geometry(tmp_path):
    res = run_case(_case(100), tmp_path, DiskSolver())
    changed = _case(400)
    changed.components[0].power = 99.0
    problem = resume_problem(changed, res.case_dir)
    assert problem and "components" in problem
    res2 = resume_case(res.case_dir, DiskSolver(), case=changed)
    assert not res2.success and "cannot resume" in res2.errors[0]


def test_nothing_to_resume(tmp_path):
    assert "no meshed run" in resume_problem(_case(), tmp_path / "missing")
