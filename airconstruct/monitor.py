"""Live numbers of a running solve, read from the solver output.

Owned by: workstream "results" (live monitoring).

:class:`RunMonitor` is fed every solver line (by runner.run_case). At the end of each
iteration it builds a :class:`Progress` snapshot:

- residuals: initial residual of every field in every region (convergence chart);
- temperatures: min / mean / max of every region in degC (min and max from the
  solver's "Min/max T" lines, the mean from the ``T_<region>`` function object);
- heat_to_air_w: heat flowing into the air through each wall patch [W] (``air_to_<part>``
  for each component, ``walls`` for the enclosure), from the ``heat_air`` object;
- openings: mass flow [kg/s, > 0 into the enclosure], temperature [degC] and p_rgh
  (gauge) of every opening; pressure_drop_pa and heat_out_w derived from them.

Snapshots go to the ``on_progress`` callback and, one JSON object per line, to
``monitor.jsonl`` in the case directory, so a UI can draw the charts live or after
the fact, and a resumed run appends to the same history.

Function object output is matched by object name (``<type> <name> write:``) and the
``<operation>(<patch>) of <field> = <value>`` / ``min/max/integ(<patch>) = a, b, c``
lines that follow, so the numbers simply stay absent if a version prints them
differently.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .case import Case

KELVIN = 273.15

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:nan|inf)"
_TIME = re.compile(r"^Time = (\d+)\s*$")
_REGION = re.compile(r"^Solving for (fluid|solid) region (\w+)")
_RES = re.compile(r"Solving for (\w+), Initial residual = (" + _NUM + r")")
_MINMAX_T = re.compile(r"^Min/max T:\s*(" + _NUM + r")\s+(" + _NUM + r")")
_FO_HEADER = re.compile(r"^(\w+) (\w+) write:\s*$")
_FO_VALUE = re.compile(r"^\s+(\w+)\((.*?)\) of (\w+) = (" + _NUM + r")\s*$")
_FO_HEAT = re.compile(r"^\s+min/max/integ\((\w+)\) = (" + _NUM + r"), (" + _NUM + r"), ("
                      + _NUM + r")")


@dataclass
class Progress:
    iteration: int
    phase: str = ""
    residuals: dict[str, dict[str, float]] = field(default_factory=dict)  # region -> field
    temperatures: dict[str, dict[str, float]] = field(default_factory=dict)  # region -> stats
    heat_to_air_w: dict[str, float] = field(default_factory=dict)  # air wall patch -> W
    openings: dict[str, dict[str, float]] = field(default_factory=dict)
    pressure_drop_pa: float = float("nan")  # inlets - outlets, area mean p_rgh
    heat_out_w: float = float("nan")  # carried out by the air through the openings

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Progress:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def part_heat_w(self) -> dict[str, float]:
        """{component: heat it gives to the air [W]} (live energy balance)."""
        return {p[len("air_to_"):]: w for p, w in self.heat_to_air_w.items()
                if p.startswith("air_to_")}


ProgressCallback = Callable[[Progress], None]


def _num(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return float("nan")


class RunMonitor:
    """Feed solver lines; emits one :class:`Progress` per iteration."""

    def __init__(self, case: Case, on_progress: ProgressCallback | None = None,
                 path: Path | None = None, cp: float | None = None):
        from .materials import GASES

        self.case = case
        self.on_progress = on_progress
        self.path = Path(path) if path is not None else None
        self.cp = cp if cp is not None else GASES[case.fluid].cp
        self.phase = ""
        self.iteration = 0
        self.last: Progress | None = None
        self._region = ""
        self._fo = ""  # function object whose output is being read
        self._reset()
        self._inlets = {o.name for o in case.openings if o.kind.value == "inlet"}
        self._outlets = {o.name for o in case.openings if o.kind.value == "outlet"}

    def _reset(self) -> None:
        self._res: dict[str, dict[str, float]] = {}
        self._minmax: dict[str, tuple[float, float]] = {}
        self._mean: dict[str, float] = {}
        self._heat: dict[str, float] = {}
        self._open: dict[str, dict[str, float]] = {}

    # ------------------------------------------------------------------ parsing
    def feed(self, line: str) -> None:
        m = _TIME.match(line)
        if m:
            self.flush()
            self.iteration = int(m.group(1))
            self._fo = ""
            return
        m = _REGION.match(line)
        if m:
            self._region, self._fo = m.group(2), ""
            return
        m = _FO_HEADER.match(line)
        if m:
            self._fo = m.group(2)
            return
        if self._fo:
            if self._fo_line(line):
                return
            if line.strip() and not line.startswith((" ", "\t")):
                self._fo = ""  # back to solver output
        m = _MINMAX_T.match(line)
        if m and self._region:
            self._minmax[self._region] = (_num(m.group(1)), _num(m.group(2)))
            return
        m = _RES.search(line)
        if m and self._region:
            reg = self._res.setdefault(self._region, {})
            f, r = m.group(1), _num(m.group(2))
            reg[f] = max(reg.get(f, 0.0), r)  # several correctors: keep the first (largest)

    def _fo_line(self, line: str) -> bool:
        name = self._fo
        m = _FO_HEAT.match(line)
        if m and name == "heat_air":
            self._heat[m.group(1)] = _num(m.group(4))
            return True
        m = _FO_VALUE.match(line)
        if not m:
            return bool(line.startswith((" ", "\t")) and line.strip())
        value = _num(m.group(4))
        kind, _, target = name.partition("_")
        if kind == "T" and target in self._outlets | self._inlets:
            self._open.setdefault(target, {})["t_c"] = value - KELVIN
        elif kind == "T":
            self._mean[target] = value
        elif kind == "flow":
            self._open.setdefault(target, {})["mass_flow_kg_s"] = -value  # phi > 0 = out
        elif kind == "p":
            self._open.setdefault(target, {})["p_pa"] = value - self.case.ambient_pressure
        return True

    # ------------------------------------------------------------------ snapshots
    def flush(self) -> Progress | None:
        if not self._res:  # e.g. function object output at solver start-up: no iteration
            self._reset()
            return None
        temps = {}
        for reg in dict.fromkeys(list(self._minmax) + list(self._mean)):
            lo, hi = self._minmax.get(reg, (math.nan, math.nan))
            temps[reg] = {"min_c": lo - KELVIN, "mean_c": self._mean.get(reg, math.nan) - KELVIN,
                          "max_c": hi - KELVIN}
        p = Progress(self.iteration, self.phase, self._res, temps, self._heat, self._open)
        p_in = [o["p_pa"] for n, o in self._open.items() if n in self._inlets and "p_pa" in o]
        p_out = [o["p_pa"] for n, o in self._open.items() if n in self._outlets and "p_pa" in o]
        if p_in and p_out:
            p.pressure_drop_pa = sum(p_in) / len(p_in) - sum(p_out) / len(p_out)
        flows = [(o["mass_flow_kg_s"], o["t_c"]) for o in self._open.values()
                 if "mass_flow_kg_s" in o and "t_c" in o]
        if flows:
            m_in = sum(m for m, _ in flows if m > 0)
            t_in = (sum(m * t for m, t in flows if m > 0) / m_in if m_in > 0
                    else self.case.ambient_temperature)
            p.heat_out_w = -sum(m * self.cp * (t - t_in) for m, t in flows)
        self._reset()
        self.last = p
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(p.to_dict(), allow_nan=True) + "\n")
        if self.on_progress:
            self.on_progress(p)
        return p


def read_history(path: Path) -> list[Progress]:
    """Every snapshot of a monitor.jsonl file (a case directory works too)."""
    path = Path(path)
    if path.is_dir():
        path = path / "monitor.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(Progress.from_dict(json.loads(line)))
        except (ValueError, TypeError):
            continue  # a line cut by a crash
    return out


def residual_series(history: list[Progress]) -> dict[str, tuple[list[int], list[float]]]:
    """{"<field>@<region>": (iterations, residuals)}: one line of a convergence chart."""
    out: dict[str, tuple[list[int], list[float]]] = {}
    for p in history:
        for reg, fields in p.residuals.items():
            for f, r in fields.items():
                its, vals = out.setdefault(f"{f}@{reg}", ([], []))
                its.append(p.iteration)
                vals.append(r)
    return out
