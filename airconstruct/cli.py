"""Command line.

  airconstruct check case.yaml      validate and show the mesh plan + time estimate
  airconstruct run case.yaml        mesh, solve (and post-process when available)
  airconstruct resume runs/<case>   continue a stopped run (--iterations to give it more)
  airconstruct queue add a.yaml b.yaml / queue run    queue runs, then run them in order
  airconstruct history              list the runs of the queue
  airconstruct compare ID1 ID2      what changed between two runs (case and results)

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
    rs = sub.add_parser("resume", help="continue a stopped run from its last iteration")
    rs.add_argument("case_dir")
    rs.add_argument("--iterations", type=int, help="new cap on the total number of iterations")
    rs.add_argument("--quiet", action="store_true", help="only print phase changes")
    q = sub.add_parser("queue", help="queue runs (add) and run the queue (run)")
    q.add_argument("action", choices=["add", "run"])
    q.add_argument("cases", nargs="*")
    q.add_argument("--label", default="")
    q.add_argument("--root", default="runs", help="history directory (default: runs)")
    q.add_argument("--quiet", action="store_true", help="only print phase changes")
    h = sub.add_parser("history", help="list the runs of the history")
    h.add_argument("--root", default="runs")
    cmp_ = sub.add_parser("compare", help="compare two runs of the history")
    cmp_.add_argument("a")
    cmp_.add_argument("b")
    cmp_.add_argument("--root", default="runs")
    a = p.parse_args(argv)
    if a.cmd in ("resume", "queue", "history", "compare"):
        return _run_control(a)
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
    _print_diagnosis(res.diagnosis)
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


def _print_diagnosis(diagnosis) -> None:
    for d in diagnosis:
        part = f" [{d.part}]" if d.part else ""
        print(f"{d.severity.upper()}{part}: {d.message}", file=sys.stderr)
        if d.hint:
            print(f"  -> {d.hint}", file=sys.stderr)


def _run_control(a) -> int:
    from .runs import RunQueue, RunStore, compare

    def on_log(line: str) -> None:
        if not a.quiet or line.startswith(("--- ", "$ ", "Converged")):
            print(line, flush=True)

    if a.cmd == "resume":
        from .runner import detect_backend, resume_case

        case = Case.load(Path(a.case_dir) / "case.yaml")
        if a.iterations:
            case.solver.iterations = a.iterations
        res = resume_case(Path(a.case_dir), detect_backend(), case=case, on_log=on_log)
        _print_diagnosis(res.diagnosis)
        if not res.success:
            print("\n".join(res.errors), file=sys.stderr)
            return 1
        print(f"{res.iterations} iterations, {'converged' if res.converged else 'not converged'}")
        return 0
    store = RunStore(a.root)
    if a.cmd == "queue" and a.action == "add":
        for f in a.cases:
            rec = store.add(Case.load(f), a.label)
            print(f"queued {rec.id}  {rec.name}")
        return 0
    if a.cmd == "queue":
        from .runner import detect_backend

        def on_event(rid, kind, payload):
            if kind == "log":
                on_log(payload)
            elif kind == "started":
                print(f"=== run {rid}", flush=True)
            elif kind == "ended":
                _print_diagnosis(payload.diagnosis)

        RunQueue(store, detect_backend, on_event).run_pending()
        return 0
    if a.cmd == "history":
        for r in store.list():
            hot = ""
            comps = (r.summary or {}).get("components", [])
            if comps:
                c = max(comps, key=lambda c: c.get("t_max_c") or -1e9)
                hot = f"  hottest {c['name']} {c['t_max_c']:.1f} degC"
            print(f"{r.id}  {r.status:<10} {r.name:<20} {r.iterations:>6} it  "
                  f"{r.label}{hot}")
        return 0
    d = compare(store.get(a.a), store.get(a.b))
    print(f"Case changes ({d.a} -> {d.b}):")
    for k, (va, vb) in d.case_changes.items():
        print(f"  {k}: {va} -> {vb}")
    print("Results:")
    for k, (va, vb, delta) in d.metrics.items():
        dd = f"  ({delta:+.3g})" if delta is not None else ""
        print(f"  {k}: {va} -> {vb}{dd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
