"""Command line: `airconstruct run case.yaml [--out runs]`, `airconstruct check case.yaml`.

Owned by: workstream "runner".
"""

from __future__ import annotations

import argparse
import sys

from .case import Case


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="airconstruct")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate a case file")
    c.add_argument("case")
    r = sub.add_parser("run", help="mesh, solve and post-process a case")
    r.add_argument("case")
    r.add_argument("--out", default="runs")
    a = p.parse_args(argv)
    case = Case.load(a.case)
    if a.cmd == "check":
        print(f"OK: {case.name} ({len(case.components)} components, {len(case.openings)} openings)")
        return 0
    raise NotImplementedError("run: implemented by the runner workstream")


if __name__ == "__main__":
    sys.exit(main())
