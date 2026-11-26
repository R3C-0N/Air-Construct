"""Run the pipeline: mesh -> setup -> solve, locally (Linux) or through WSL2 (Windows).

Owned by: workstream "runner".

Public interface (used by cli and ui):
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .case import Case

LogCallback = Callable[[str], None]  # one line of solver output
ResidualCallback = Callable[[int, dict[str, float]], None]  # iteration, {field: residual}


@dataclass
class RunResult:
    case_dir: Path
    success: bool
    converged: bool
    iterations: int
    wall_time_s: float
    errors: list[str] = field(default_factory=list)


class Backend:
    """How OpenFOAM commands are executed. Subclasses: LocalBackend (OpenFOAM on PATH
    or sourced from a bashrc), WslBackend (Windows: wsl.exe -e bash -lc ..., case dir
    kept inside the WSL filesystem)."""

    def run(self, argv: list[str], cwd: Path, on_line: LogCallback | None = None) -> int:
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        """(ok, human readable message), e.g. OpenFOAM version found or what is missing."""
        raise NotImplementedError


def detect_backend() -> Backend:
    """Pick LocalBackend on Linux, WslBackend on Windows."""
    raise NotImplementedError


def run_case(
    case: Case,
    work_dir: Path,
    backend: Backend | None = None,
    on_log: LogCallback | None = None,
    on_residual: ResidualCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RunResult:
    """Full pipeline in work_dir/<case.name>: write mesh dicts, mesh, write setup and
    fields, decompose if cores>1, solve, reconstruct. Stops early on convergence or
    should_stop()."""
    raise NotImplementedError
