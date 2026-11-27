"""Run the pipeline: mesh -> setup -> solve, locally (Linux) or through WSL2 (Windows).

Owned by: workstream "runner".
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .case import Case

LogCallback = Callable[[str], None]  # one line of solver output
ResidualCallback = Callable[[int, dict[str, float]], None]  # iteration, {field: residual}

BASHRC_CANDIDATES = [
    "/usr/lib/openfoam/openfoam*/etc/bashrc",  # openfoam.com packages
    "/opt/openfoam*/etc/bashrc",
    "/usr/share/openfoam/etc/bashrc",  # Debian/Ubuntu "openfoam" package
]


@dataclass
class RunResult:
    case_dir: Path
    success: bool
    converged: bool
    iterations: int
    wall_time_s: float
    errors: list[str] = field(default_factory=list)
    # last "Min/max T" printed per region, in degC: {region: (t_min, t_max)}
    region_temperatures: dict[str, tuple[float, float]] = field(default_factory=dict)
    # last live snapshot (monitor.Progress): residuals per region and field, min/mean/max
    # temperature per region, heat to the air per part, flow through the openings
    progress: object | None = None
    stopped: bool = False  # ended by should_stop(): can be resumed (resume_case)
    diagnosis: list = field(default_factory=list)  # diagnose.Diagnosis, when it went wrong


class Backend:
    """How OpenFOAM commands are executed."""

    def run(self, argv: list[str], cwd: Path, on_line: LogCallback | None = None) -> int:
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def write_now(self) -> None:
        """Make the running solver write its state and exit cleanly."""
        raise NotImplementedError

    def stop(self) -> None:
        """Terminate the running command (fallback when write_now is ignored)."""


def find_bashrc() -> str | None:
    env = os.environ.get("AIRCONSTRUCT_FOAM_BASHRC")
    if env:
        return env
    for pat in BASHRC_CANDIDATES:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]  # newest version
    return None


class LocalBackend(Backend):
    """OpenFOAM on this Linux machine. If the tools are not on PATH, each command is
    wrapped in bash after sourcing the OpenFOAM bashrc."""

    def __init__(self, bashrc: str | None = None):
        self.bashrc = bashrc if bashrc is not None else (
            None if os.environ.get("WM_PROJECT_DIR") else find_bashrc()
        )

    def _argv(self, argv: list[str]) -> list[str]:
        if not self.bashrc:
            return argv
        cmd = " ".join(shlex.quote(a) for a in argv)
        # exec: the OpenFOAM process replaces bash, so signals reach it directly
        return ["bash", "-c", f"source {shlex.quote(self.bashrc)} >/dev/null 2>&1; exec {cmd}"]

    def run(self, argv: list[str], cwd: Path, on_line: LogCallback | None = None) -> int:
        proc = subprocess.Popen(
            self._argv(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self.proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip("\n"))
        return proc.wait()

    def stop(self) -> None:
        p = getattr(self, "proc", None)
        if p and p.poll() is None:
            p.terminate()

    def write_now(self) -> None:
        """Ask the running solver to write its current state and exit (SIGUSR1, see
        OptimisationSwitches.stopAtWriteNowSignal in the generated controlDict)."""
        p = getattr(self, "proc", None)
        if p and p.poll() is None:
            p.send_signal(signal.SIGUSR1)

    def available(self) -> tuple[bool, str]:
        if not self.bashrc and not shutil.which("blockMesh"):
            return False, "OpenFOAM not found (install openfoam.com, or set AIRCONSTRUCT_FOAM_BASHRC)"
        lines: list[str] = []
        script = 'command -v chtMultiRegionSimpleFoam >/dev/null && echo "OpenFOAM ${WM_PROJECT_VERSION:-}"'
        probe = ["sh", "-c", script]
        try:
            code = self.run(probe, Path.cwd(), lines.append)
        except OSError as e:
            return False, str(e)
        if code != 0:
            return False, "OpenFOAM found but chtMultiRegionSimpleFoam is missing"
        return True, " ".join(lines).strip() or "OpenFOAM found"


def detect_backend() -> Backend:
    if sys.platform.startswith("win"):
        try:
            from .wsl import WslBackend  # workstream "wsl"
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("WSL backend not available yet") from e
        return WslBackend()
    return LocalBackend()


END_TIME_MARGIN = 1_000_000  # see run_phase
WRITE_NOW_GRACE = 50  # iterations to wait for write_now before stop()

_RES = re.compile(r"Solving for (\w+), Initial residual = ([0-9.eE+-]+)")
_TIME = re.compile(r"^Time = (\d+)")
_REGION = re.compile(r"^Solving for (?:fluid|solid) region (\w+)")
_MINMAX_T = re.compile(r"^Min/max T:\s*([0-9.eE+-]+)\s+([0-9.eE+-]+)")


class ResidualParser:
    """Feed solver lines; calls on_residual(iteration, {field: max initial residual}).

    Also tracks the temperature range of every region and decides convergence:
    fluid residuals (U*, p_rgh) below ``tol`` and every region's max temperature
    stable within ``t_window_k`` over the last ``window`` iterations. Solid ``h``
    residuals are ignored: in small, nearly isothermal parts they stay high even
    when the temperature no longer moves.
    """

    def __init__(self, on_residual: ResidualCallback | None = None, tol: float = 1e-3,
                 window: int = 100, t_window_k: float = 0.02,
                 t_rel: float = 5e-4):
        self.on_residual = on_residual
        self.iteration = 0
        self.current: dict[str, float] = {}
        self.last: dict[str, float] = {}
        self.tol, self.window, self.t_window_k, self.t_rel = tol, window, t_window_k, t_rel
        self.region = ""
        self.fluid_regions: set[str] = set()
        self.fluid_res: dict[str, float] = {}
        self.temps: dict[str, tuple[float, float]] = {}  # Kelvin
        self.t_history: list[tuple[int, dict[str, float]]] = []

    def feed(self, line: str) -> None:
        m = _TIME.match(line)
        if m:
            self._flush()
            self.iteration = int(m.group(1))
            return
        m = _REGION.match(line)
        if m:
            self.region = m.group(1)
            if "fluid" in line:
                self.fluid_regions.add(self.region)
            return
        m = _MINMAX_T.match(line)
        if m and self.region:
            self.temps[self.region] = (float(m.group(1)), float(m.group(2)))
            return
        m = _RES.search(line)
        if m:
            f, r = m.group(1), float(m.group(2))
            self.current[f] = max(self.current.get(f, 0.0), r)
            if self.region in self.fluid_regions:
                self.fluid_res[f] = r

    def _flush(self) -> None:
        if self.current:
            self.last = self.current
            if self.on_residual:
                self.on_residual(self.iteration, dict(self.current))
        if self.temps:
            self.t_history.append((self.iteration, {r: t[1] for r, t in self.temps.items()}))
            if len(self.t_history) > self.window + 1:
                del self.t_history[0]
        self.current = {}

    def flow_converged(self) -> bool:
        flow = [r for f, r in self.fluid_res.items() if f.startswith("U") or f == "p_rgh"]
        return bool(flow) and max(flow) <= self.tol

    def temperatures_stable(self) -> bool:
        if len(self.t_history) <= self.window:
            return False
        (_, old), (_, new) = self.t_history[0], self.t_history[-1]
        t_ref = min(t[0] for t in self.temps.values())  # ~ ambient / inlet temperature
        return all(
            abs(new[r] - old.get(r, 0.0)) < max(self.t_window_k, self.t_rel * (new[r] - t_ref))
            for r in new
        )

    def reset_window(self) -> None:
        self.t_history.clear()
        self.fluid_res.clear()

    def converged(self) -> bool:
        return self.flow_converged() and self.temperatures_stable()

    def max_temperatures(self) -> dict[str, float]:
        return {r: t[1] for r, t in self.temps.items()}

    def temperatures_c(self) -> dict[str, tuple[float, float]]:
        return {r: (a - 273.15, b - 273.15) for r, (a, b) in self.temps.items()}

    def close(self) -> None:
        self._flush()


def run_case(
    case: Case,
    work_dir: Path,
    backend: Backend | None = None,
    on_log: LogCallback | None = None,
    on_residual: ResidualCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable | None = None,
    monitors: bool = True,
    resume: bool = False,
    case_dir: Path | None = None,
) -> RunResult:
    """Full pipeline in work_dir/<case.name>. Returns when the solver ends.

    Solve strategy (steady conjugate heat transfer):
      1. "flow": flow + energy until the flow residuals converge.
      2. "energy": frozen flow, energy only (about 4x cheaper per iteration) until every
         region's max temperature is stable. The partitioned fluid/solid coupling makes
         this the slow part.
      3. "check": flow unfrozen again for a short while, so buoyancy can react to the new
         temperatures. If temperatures moved, go back to 2 (at most 3 cycles).
    case.solver.iterations caps the total number of iterations.

    on_progress(monitor.Progress) is called after every iteration with the residuals of
    every field in every region, the min/mean/max temperature of every region, the
    heat each part gives to the air and the flow through the openings (OpenFOAM
    function objects, see foam/monitors.py); the same snapshots are appended to
    <case_dir>/monitor.jsonl. monitors=False leaves the function objects out.

    case_dir overrides work_dir/<case.name>. resume=True continues the run already in
    case_dir from its latest written iteration (after a stop, a crash or to give it more
    iterations: raise case.solver.iterations) instead of starting over; only solver
    settings may differ from the case it was started with. The state of the run
    (running, stopped, converged...) is kept in <case_dir>/run.json, see read_state.
    """
    from .foam.monitors import write_monitors
    from .foam.writer import SOLVER_APP, write_case_setup, write_initial_fields
    from .mesh import mesh_commands, write_mesh_dicts
    from .monitor import RunMonitor

    backend = backend or detect_backend()
    case_dir = Path(case_dir) if case_dir is not None else Path(work_dir) / case.name
    t0 = time.time()
    errors: list[str] = []
    log_lines: list[str] = []
    parser = ResidualParser(on_residual, tol=case.solver.convergence_tol * 10)
    state = {"status": "running", "started_at": _now(), "resumes": 0, "wall_time_s": 0.0}
    start_it = 0
    if resume:
        problem = resume_problem(case, case_dir)
        if problem:
            return RunResult(case_dir, False, False, 0, 0.0, [f"cannot resume: {problem}"])
        previous = read_state(case_dir)
        state.update({k: previous[k] for k in ("started_at", "resumes", "wall_time_s")
                      if k in previous})
        state["resumes"] += 1
        start_it = latest_iteration(case_dir)
    else:
        if case_dir.exists():
            shutil.rmtree(case_dir)
        case_dir.mkdir(parents=True)
    case.save(case_dir / "case.yaml")
    parser.iteration = start_it
    monitor = RunMonitor(case, on_progress, case_dir / "monitor.jsonl")
    monitor.iteration = start_it
    wall0 = state["wall_time_s"]

    def save_state(**kw) -> None:
        state.update(kw, iteration=parser.iteration, updated_at=_now(),
                     wall_time_s=wall0 + time.time() - t0)
        write_state(case_dir, state)

    save_state()

    def log(line: str) -> None:
        log_lines.append(line)
        if len(log_lines) > 200:
            del log_lines[:100]
        if on_log:
            on_log(line)

    def result(ok: bool, converged: bool = False) -> RunResult:
        status = ("failed" if not ok else "converged" if converged
                  else "stopped" if stopped else "finished")
        save_state(status=status)
        res = RunResult(case_dir, ok, converged, parser.iteration, time.time() - t0, errors,
                        parser.temperatures_c(), monitor.last, stopped and ok)
        if status != "converged":
            try:
                from .diagnose import diagnose

                res.diagnosis = diagnose(case, case_dir, res)
            except Exception as e:  # noqa: BLE001 (a diagnosis must never hide the result)
                log(f"(diagnosis failed: {e})")
        return res

    def step(argv: list[str], logname: str, on_line_extra: Callable[[str], None] | None = None,
             mode: str = "w") -> bool:
        log(f"$ {' '.join(argv)}")
        ended = False
        with open(case_dir / f"log.{logname}", mode) as fh:

            def on_line(line: str) -> None:
                nonlocal ended
                ended = ended or line.strip() == "End"
                fh.write(line + "\n")
                log(line)
                if on_line_extra:
                    on_line_extra(line)

            code = backend.run(argv, case_dir, on_line)
        if code != 0 and ended:
            # The solver finished ("End") and crashed while exiting: the solver output is
            # read through a buffered pipe, so a write_now signal can arrive after the
            # solver already reached endTime and is tearing down. Its results are written.
            log(f"(ignoring exit {code} after a clean End)")
            return True
        if code != 0:
            tail = "\n".join(log_lines[-30:])
            errors.append(f"{' '.join(argv)} failed (exit {code})\n{tail}")
        return code == 0

    stopped = False
    write_case_setup(case, case_dir)
    write_monitors(case, case_dir, monitors)
    if resume:
        log(f"--- resuming from iteration {start_it}")
    else:
        write_mesh_dicts(case, case_dir)
        for argv in mesh_commands(case):
            name = argv[0] + ("." + argv[-1].split(".")[-1] if argv[0] == "topoSet" else "")
            if not step(argv, name):
                return result(False)
        write_initial_fields(case, case_dir)
        from .diagnose import check_case, check_mesh

        for d in check_case(case) + check_mesh(case, case_dir):
            log(f"--- {d.severity}: {d.message}")

    cores = max(1, case.solver.cores)
    if cores > 1:
        decompose = ["decomposePar", "-allRegions", "-force"] + (["-latestTime"] if resume else [])
        if not step(decompose, "decomposePar"):
            return result(False)
        solve = ["mpirun", "--oversubscribe", "-np", str(cores), SOLVER_APP, "-parallel"]
    else:
        solve = [SOLVER_APP]
    # SIGUSR1 (10) = write the current state and stop; how phases are ended cleanly
    solve += ["-opt-switch", "stopAtWriteNowSignal=10"]

    control = case_dir / "system" / "controlDict"
    fv_air = case_dir / "system" / case.fluid_region / "fvSolution"
    budget = case.solver.iterations
    monitor_state = {"on": monitors}

    def set_phase(frozen: bool, end: int) -> None:
        txt = control.read_text()
        txt = re.sub(r"stopAt\s+\w+;", "stopAt          endTime;", txt)
        txt = re.sub(r"endTime\s+[0-9.eE+-]+;", f"endTime         {end};", txt)
        control.write_text(txt)
        fv = fv_air.read_text()
        fv_air.write_text(re.sub(r"frozenFlow\s+\w+;", f"frozenFlow      {'true' if frozen else 'false'};", fv))

    def run_phase(name: str, frozen: bool, max_its: int, done: Callable[[], bool]) -> bool:
        """Run the solver until done() (checked after each iteration) or max_its."""
        nonlocal stopped
        start = parser.iteration
        end = min(budget, start + max_its)
        if end <= start:
            return True
        # endTime far past the cap: a phase always ends through write_now (so the last
        # state is written whatever the writeInterval) and never by reaching endTime, so
        # the signal cannot land while the solver is already exiting, however late the
        # watcher reads the output. stop() is the fallback if the signal gets lost.
        set_phase(frozen, end + END_TIME_MARGIN)
        parser.reset_window()
        requested = {"v": False}
        log(f"--- phase {name}: iterations {start}..{end}{' (frozen flow)' if frozen else ''}")
        monitor.phase = name
        save_state(phase=name)

        def watch(line: str) -> None:
            nonlocal stopped
            parser.feed(line)
            monitor.feed(line)
            if not line.startswith("ExecutionTime"):
                return
            if requested["v"]:
                if parser.iteration >= requested["at"] + WRITE_NOW_GRACE:
                    log("--- solver ignored the write-now signal, stopping it")
                    backend.stop()
                return
            user_stop = bool(should_stop and should_stop())
            if user_stop or done() or parser.iteration >= end:
                stopped = stopped or user_stop
                requested["v"] = True
                requested["at"] = parser.iteration
                backend.write_now()

        n_errors = len(errors)
        ok = step(solve, SOLVER_APP, watch, mode="a")
        if not ok and parser.iteration == start and monitor_state["on"]:
            # some OpenFOAM builds cannot run function objects at all (e.g. the Debian
            # v1912 package fails in functionObjectList::read): retry without them
            monitor_state["on"] = False
            del errors[n_errors:]
            log("--- the solver failed to start with the live monitors, retrying without them")
            write_monitors(case, case_dir, False)
            ok = step(solve, SOLVER_APP, watch, mode="a")
        parser.close()
        monitor.flush()
        return ok

    converged = False
    if not run_phase("flow", False, budget, parser.flow_converged):
        return result(False)
    for _ in range(3):
        if stopped or parser.iteration >= budget:
            break
        if not run_phase("energy", True, budget, parser.temperatures_stable):
            return result(False)
        if stopped or parser.iteration >= budget:
            break
        before = parser.max_temperatures()
        if not run_phase("check", False, 200, lambda: False):
            return result(False)
        if stopped:  # a check cut short says nothing about convergence
            break
        after = parser.max_temperatures()
        drift = max(abs(after[r] - before.get(r, after[r])) for r in after)
        log(f"--- buoyancy check: max temperature change {drift:.3f} K")
        rise = max(after.values()) - min(t[0] for t in parser.temps.values())
        if drift < max(0.2, 0.01 * rise):  # buoyancy moved temperatures by < 1 % of the rise
            converged = parser.flow_converged()
            break
    if cores > 1 and not step(["reconstructPar", "-allRegions", "-latestTime"], "reconstructPar"):
        return result(False)
    if converged:
        log(f"Converged after {parser.iteration} iterations")
    return result(True, converged)


# --------------------------------------------------------------------------- run state

STATE_FILE = "run.json"


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_state(case_dir: Path, state: dict) -> None:
    import json

    path = Path(case_dir) / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def read_state(case_dir: Path) -> dict:
    """The run's state: {"status": "running" | "stopped" | "converged" | "finished" |
    "failed", "iteration", "phase", "started_at", "updated_at", "wall_time_s",
    "resumes"}. {} when the directory holds no run."""
    import json

    try:
        return json.loads((Path(case_dir) / STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}


def latest_iteration(case_dir: Path) -> int:
    """Latest iteration written (reconstructed, else in processor0); 0 if none."""
    best = 0
    for base in (Path(case_dir), Path(case_dir) / "processor0"):
        if not base.is_dir():
            continue
        for d in base.iterdir():
            try:
                v = float(d.name)
            except ValueError:
                continue
            if d.is_dir() and v > best and any(d.iterdir()):
                best = v
    return int(best)


def resume_problem(case: Case, case_dir: Path) -> str | None:
    """Why the run in case_dir cannot be resumed with `case` (None if it can)."""
    case_dir = Path(case_dir)
    saved = case_dir / "case.yaml"
    if not saved.exists() or not (case_dir / "constant" / case.fluid_region / "polyMesh").is_dir():
        return f"no meshed run in {case_dir}"
    if latest_iteration(case_dir) <= 0:
        return "no iteration was written yet, start it again"
    old = Case.load(saved).model_dump(mode="json", exclude={"solver"})
    new = case.model_dump(mode="json", exclude={"solver"})
    if old != new:
        changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        return ("the case changed since the run started (" + ", ".join(changed)
                + "): only solver settings can change when resuming")
    if read_state(case_dir).get("status") == "converged" and case.solver.iterations <= \
            latest_iteration(case_dir):
        return "the run already converged"
    return None


def resume_case(case_dir: Path, backend: Backend | None = None, case: Case | None = None,
                **kwargs) -> RunResult:
    """Continue the run in case_dir (stopped, crashed, or out of iterations). `case`
    defaults to the saved case.yaml; pass it to change solver settings, e.g. more
    iterations."""
    case_dir = Path(case_dir)
    case = case if case is not None else Case.load(case_dir / "case.yaml")
    return run_case(case, case_dir.parent, backend, resume=True, case_dir=case_dir, **kwargs)
