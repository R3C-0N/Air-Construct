"""Read the probe histories written by the solver: temperature (and air velocity) at
user-placed points, every ``solver.probe_interval`` iterations.

Owned by: workstream "results". Works while the solver runs (the UI can poll it) and
after. OpenFOAM v2406 writes postProcessing/probes_<region>/<region>/<start time>/<field>,
a new <start time> folder each time the runner restarts the solver for a new phase;
they are stitched together here. Columns are matched to the case's probes by
position, so a probe OpenFOAM could not place (outside its region's mesh) is
reported as missing instead of shifting the others.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .case import Case
from .foam.writer import probe_function_name

KELVIN = 273.15
_HEADER = re.compile(r"^#\s*Probe\s+\d+\s*\(([^)]*)\)")


@dataclass
class ProbeSeries:
    name: str
    region: str
    point: tuple[float, float, float]
    iterations: list[int] = field(default_factory=list)
    t_c: list[float] = field(default_factory=list)  # [degC]
    u: list[tuple[float, float, float]] = field(default_factory=list)  # [m/s], air only

    @property
    def speed(self) -> list[float]:
        return [float(np.linalg.norm(v)) for v in self.u]

    def last(self) -> tuple[float, float]:
        """(temperature degC, speed m/s) at the last sample, nan when missing."""
        t = self.t_c[-1] if self.t_c else float("nan")
        s = self.speed[-1] if self.u else float("nan")
        return t, s


def _start_dirs(case_dir: Path, fo: str) -> list[Path]:
    """Start-time folders of a probes function object. OpenFOAM v2406 writes
    postProcessing/probes_<region>/<region>/<t>/; other versions put the region first
    or leave it out, so look one level below the function object's folder too."""
    dirs = []
    for base in (case_dir / "postProcessing").glob(f"**/{fo}"):
        for d in [*base.iterdir(), *(c for b in base.iterdir() if b.is_dir() for c in b.iterdir())]:
            if not d.is_dir():
                continue
            try:
                float(d.name)
            except ValueError:
                continue
            dirs.append(d)
    return sorted(dirs, key=lambda d: float(d.name))


def _parse(path: Path) -> tuple[list[tuple[float, ...]], dict[int, list[str]]]:
    """(probe points listed in the header, {iteration: raw values per probe})."""
    points: list[tuple[float, ...]] = []
    rows: dict[int, list[str]] = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return points, rows
    for line in lines:
        if line.startswith("#"):
            m = _HEADER.match(line)
            if m:
                points.append(tuple(float(v) for v in m.group(1).split()))
            continue
        line = line.strip()
        if not line:
            continue
        if "(" in line:  # vectors: "10 (1 2 3) (4 5 6)"
            head, *vecs = re.split(r"\s*\(", line)
            vals = [v.rstrip(") ").strip() for v in vecs]
            t = head.strip()
        else:
            t, *vals = line.split()
        if points and len(vals) != len(points):  # a line cut while the solver writes it
            continue
        try:
            rows[round(float(t))] = vals
        except ValueError:
            continue
    return points, rows


def read_probes(case: Case, case_dir: str | Path) -> dict[str, ProbeSeries]:
    """{probe name: history}, every probe of the case (empty history if not written yet)."""
    case_dir = Path(case_dir)
    out = {p.name: ProbeSeries(p.name, case.probe_region(p), p.point.as_tuple())
           for p in case.probes}
    regions = {s.region for s in out.values()}
    for region in regions:
        mine = [p for p in case.probes if case.probe_region(p) == region]
        fo = probe_function_name(region)
        t_rows: dict[int, list[str]] = {}
        u_rows: dict[int, list[str]] = {}
        cols: list[int | None] = []
        for d in _start_dirs(case_dir, fo):
            for fname, rows in (("T", t_rows), ("U", u_rows)):
                points, data = _parse(d / fname)
                if fname == "T" and points:
                    cols = [_column(points, p.point.as_tuple()) for p in mine]
                rows.update(data)
        if not cols and t_rows:  # no readable header: OpenFOAM keeps the given order
            cols = list(range(len(mine)))
        if not cols:
            continue
        for p, col in zip(mine, cols, strict=True):
            s = out[p.name]
            if col is None:
                continue
            for it in sorted(t_rows):
                vals = t_rows[it]
                if col >= len(vals):
                    continue
                s.iterations.append(it)
                s.t_c.append(float(vals[col]) - KELVIN)
                uv = u_rows.get(it)
                if uv is not None and col < len(uv):
                    s.u.append(tuple(float(x) for x in uv[col].split()))  # type: ignore[arg-type]
    return out


def _column(points: list[tuple[float, ...]], pt: tuple[float, float, float]) -> int | None:
    for i, q in enumerate(points):
        if len(q) == 3 and all(abs(a - b) <= 1e-7 + 1e-5 * abs(b) for a, b in zip(q, pt, strict=True)):
            return i
    return None
