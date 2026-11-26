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


class Backend:
    """How OpenFOAM commands are executed."""

    def run(self, argv: list[str], cwd: Path, on_line: LogCallback | None = None) -> int:
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def write_now(self) -> None:
        """Make the running solver write its state and exit cleanly."""
        raise NotImplementedError


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
        try:
            code = self.run(["foamVersion"], Path.cwd(), lines.append)
        except OSError as e:
            return False, str(e)
        if code != 0 and self.bashrc:
            return True, f"OpenFOAM via {self.bashrc}"
        return code == 0, " ".join(lines).strip() or "OpenFOAM found"


def detect_backend() -> Backend:
    if sys.platform.startswith("win"):
        try:
            from .wsl import WslBackend  # workstream "wsl"
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("WSL backend not available yet") from e
        return WslBackend()
    return LocalBackend()


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
    """
    from .foam.writer import SOLVER_APP, write_case_setup, write_initial_fields
    from .mesh import mesh_commands, write_mesh_dicts

    backend = backend or detect_backend()
    case_dir = Path(work_dir) / case.name
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    case.save(case_dir / "case.yaml")
    t0 = time.time()
    errors: list[str] = []
    log_lines: list[str] = []
    parser = ResidualParser(on_residual, tol=case.solver.convergence_tol * 10)

    def log(line: str) -> None:
        log_lines.append(line)
        if len(log_lines) > 200:
            del log_lines[:100]
        if on_log:
            on_log(line)

    def result(ok: bool, converged: bool = False) -> RunResult:
        return RunResult(case_dir, ok, converged, parser.iteration, time.time() - t0, errors,
                         parser.temperatures_c())

    def step(argv: list[str], logname: str, on_line_extra: Callable[[str], None] | None = None,
             mode: str = "w") -> bool:
        log(f"$ {' '.join(argv)}")
        with open(case_dir / f"log.{logname}", mode) as fh:

            def on_line(line: str) -> None:
                fh.write(line + "\n")
                log(line)
                if on_line_extra:
                    on_line_extra(line)

            code = backend.run(argv, case_dir, on_line)
        if code != 0:
            tail = "\n".join(log_lines[-30:])
            errors.append(f"{' '.join(argv)} failed (exit {code})\n{tail}")
        return code == 0

    write_case_setup(case, case_dir)
    write_mesh_dicts(case, case_dir)
    for argv in mesh_commands(case):
        name = argv[0] + ("." + argv[-1].split(".")[-1] if argv[0] == "topoSet" else "")
        if not step(argv, name):
            return result(False)
    write_initial_fields(case, case_dir)

    cores = max(1, case.solver.cores)
    if cores > 1:
        if not step(["decomposePar", "-allRegions", "-force"], "decomposePar"):
            return result(False)
        solve = ["mpirun", "--oversubscribe", "-np", str(cores), SOLVER_APP, "-parallel"]
    else:
        solve = [SOLVER_APP]
    # SIGUSR1 (10) = write the current state and stop; how phases are ended cleanly
    solve += ["-opt-switch", "stopAtWriteNowSignal=10"]

    control = case_dir / "system" / "controlDict"
    fv_air = case_dir / "system" / case.fluid_region / "fvSolution"
    budget = case.solver.iterations
    stopped = False

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
        # endTime a bit past the cap: the phase always ends through write_now, so the
        # last state is written whatever the writeInterval
        set_phase(frozen, end + 10)
        parser.reset_window()
        requested = {"v": False}
        log(f"--- phase {name}: iterations {start}..{end}{' (frozen flow)' if frozen else ''}")

        def watch(line: str) -> None:
            nonlocal stopped
            parser.feed(line)
            if not line.startswith("ExecutionTime") or requested["v"]:
                return
            user_stop = bool(should_stop and should_stop())
            if user_stop or done() or parser.iteration >= end:
                stopped = stopped or user_stop
                requested["v"] = True
                backend.write_now()

        ok = step(solve, SOLVER_APP, watch, mode="a")
        parser.close()
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
