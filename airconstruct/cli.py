"""Command line.

  airconstruct check case.yaml      validate and show the mesh plan + time estimate
  airconstruct run case.yaml        mesh, solve (and post-process when available)

Owned by: core pipeline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .case import Case
from .estimate import estimate_runtime_s, format_duration
from .fans import estimate_flow
from .mesh import plan_mesh
from .thermal_estimate import estimate_temperatures


def _check(case: Case) -> None:
    plan = plan_mesh(case)
    print(f"Case {case.name}: {len(case.components)} components, {len(case.openings)} openings")
    print(f"Mesh: {plan.divisions[0]} x {plan.divisions[1]} x {plan.divisions[2]} = "
          f"{plan.n_cells:,} cells (precision level {case.precision.level})")
    print(f"Estimated run time: {format_duration(estimate_runtime_s(case, plan.n_cells))} "
          f"on {case.solver.cores} core(s)")
    for w in plan.warnings:
        print(f"Warning: {w}")
    est = estimate_flow(case)
    print(f"Air flow (1D estimate): {est.flow_rate * 3600:.1f} m3/h, "
          f"system pressure loss {est.pressure_drop:.1f} Pa")
    for f in est.fans:
        print(f"  fan {f.name:<12} {f.flow_rate * 3600:7.1f} m3/h at {f.pressure:6.1f} Pa")
    print("Instant estimate (lumped model, rough):")
    for name, t in estimate_temperatures(case).items():
        print(f"  {name:<16} {t:7.1f} degC")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="airconstruct")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate a case file, show mesh size and time estimate")
    c.add_argument("case")
    r = sub.add_parser("run", help="mesh, solve and post-process a case")
    r.add_argument("case")
    r.add_argument("--out", default="runs", help="work directory (default: runs)")
    r.add_argument("--level", type=int, help="override the precision level (1-5)")
    r.add_argument("--cores", type=int, help="override the number of cores")
    r.add_argument("--quiet", action="store_true", help="only print phase changes")
    a = p.parse_args(argv)
    case = Case.load(a.case)
    if a.cmd == "check":
        _check(case)
        return 0

    if a.level:
        case.precision.level = a.level
    if a.cores:
        case.solver.cores = a.cores
    from .runner import detect_backend, run_case

    backend = detect_backend()
    ok, msg = backend.available()
    if not ok:
        print(f"Error: {msg}", file=sys.stderr)
        return 2
    _check(case)

    def on_log(line: str) -> None:
        if not a.quiet or line.startswith(("--- ", "$ ", "Converged")):
            print(line, flush=True)

    res = run_case(case, Path(a.out), backend, on_log=on_log)
    if not res.success:
        print("\n".join(res.errors), file=sys.stderr)
        return 1
    print(f"\nDone in {res.wall_time_s:.0f} s, {res.iterations} iterations, "
          f"{'converged' if res.converged else 'NOT fully converged (see the log)'}")
    print("Max temperature per region:")
    for region, (_, tmax) in res.region_temperatures.items():
        print(f"  {region:<16} {tmax:7.1f} degC")
    try:
        from .results import write_report

        report = write_report(case, res.case_dir, res.case_dir / "report")
        print(f"Report: {report}")
    except (ImportError, NotImplementedError):
        pass
    print(f"Case directory: {res.case_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
