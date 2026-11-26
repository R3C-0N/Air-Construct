"""Instant 1D estimate of the air flow through the enclosure, fan operating points included.

Owned by: core pipeline. Used before any CFD: initial turbulence levels and the lumped
thermal estimate need a flow rate, and the UI can show the expected fan operating point
right away. The CFD result replaces it (results.summarize reports the real one).

Model: the enclosure is one duct. Every inlet is in parallel, every outlet is in
parallel; each side loses (K + K_port) * rho * u^2 / 2 at its mean velocity, K being
the area-weighted grille loss coefficient and K_port 0.5 at the entry (sharp entrance)
and 1.0 at the exit (the jet's dynamic pressure is lost). Fans with a curve run where
the sum of their curves meets that resistance. Imposed flow rates (inlets with a flow
rate, fans with a flow rate) win over curves. Buoyancy is ignored, so a case driven
only by free vents gets no flow.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from .case import Case, Fan, Opening, OpeningKind
from .materials import GASES

R_GAS = 8.314462618
K_ENTRY, K_EXIT = 0.5, 1.0


@dataclass
class FanPoint:
    name: str  # opening name
    flow_rate: float  # [m3/s]
    pressure: float  # static pressure rise [Pa]


@dataclass
class FlowEstimate:
    flow_rate: float  # total volumetric flow through the enclosure [m3/s]
    fans: list[FanPoint]
    pressure_drop: float  # system pressure loss at that flow [Pa]


def air_density(case: Case, t_c: float | None = None) -> float:
    gas = GASES[case.fluid]
    t = case.ambient_temperature if t_c is None else t_c
    return case.ambient_pressure * gas.mol_weight * 1e-3 / (R_GAS * (t + 273.15))


def _side(openings: list[Opening], k_port: float) -> tuple[float, float]:
    """(total area, effective loss coefficient) of parallel openings."""
    area = sum(o.area() for o in openings)
    if area <= 0:
        return 0.0, 0.0
    k = sum(o.area() * (o.grille.k() if o.grille else 0.0) for o in openings) / area
    return area, k + k_port


def system_loss(case: Case, q: float) -> float:
    """Pressure the enclosure needs [Pa] to pass a total flow q [m3/s]."""
    rho = air_density(case)
    loss = 0.0
    for kind, k_port in ((OpeningKind.inlet, K_ENTRY), (OpeningKind.outlet, K_EXIT)):
        a, k = _side([o for o in case.openings if o.kind == kind], k_port)
        if a > 0:
            loss += k * 0.5 * rho * (q / a) ** 2
    return loss


def _curve_flow(fan: Fan, dp: float) -> float:
    """Flow rate [m3/s] a fan with a curve delivers against dp (0 past shut-off)."""
    assert fan.curve is not None
    c = fan.curve
    if dp >= c[0][1]:
        return c[0][0]
    for (q0, p0), (q1, p1) in itertools.pairwise(c):
        if p1 <= dp <= p0:
            return q0 if p0 == p1 else q0 + (q1 - q0) * (p0 - dp) / (p0 - p1)
    return c[-1][0]


def estimate_flow(case: Case) -> FlowEstimate:
    fixed_in = [o for o in case.openings if o.kind == OpeningKind.inlet and o.fixed_flow]
    fixed_out = [o for o in case.openings if o.kind == OpeningKind.outlet and o.fixed_flow]
    curves = [o for o in case.openings if o.fan_curve]
    if fixed_in or fixed_out:
        q_in = sum(o.normal_velocity() * o.area() for o in fixed_in)
        q = q_in if fixed_in else sum(o.normal_velocity() * o.area() for o in fixed_out)
        dp = system_loss(case, q)
        fans = [FanPoint(o.name, o.normal_velocity() * o.area(), dp)
                for o in fixed_in + fixed_out if o.fan is not None]
        # curve fans in a case with an imposed flow: report them at that pressure
        fans += [FanPoint(o.name, _curve_flow(o.fan, dp), dp) for o in curves]  # type: ignore[arg-type]
        return FlowEstimate(q, fans, dp)
    if not curves:
        return FlowEstimate(0.0, [], 0.0)

    def total(dp: float) -> float:
        return sum(_curve_flow(o.fan, dp) for o in curves)  # type: ignore[arg-type]

    # bisection on the pressure: the fans' flow falls and the system's need rises with it
    lo, hi = 0.0, max(o.fan.curve[0][1] for o in curves)  # type: ignore[union-attr]
    for _ in range(100):
        mid = (lo + hi) / 2
        if system_loss(case, total(mid)) > mid:
            lo = mid
        else:
            hi = mid
    dp = (lo + hi) / 2
    fans = [FanPoint(o.name, _curve_flow(o.fan, dp), dp) for o in curves]  # type: ignore[arg-type]
    return FlowEstimate(total(dp), fans, dp)


def opening_velocity(case: Case, o: Opening, est: FlowEstimate | None = None) -> float:
    """Expected mean speed [m/s] through an opening (imposed, else estimated)."""
    if o.fixed_flow:
        return abs(o.normal_velocity())
    est = est or estimate_flow(case)
    for f in est.fans:
        if f.name == o.name:
            return f.flow_rate / o.area()
    side = [p for p in case.openings if p.kind == o.kind]
    area = sum(p.area() for p in side)
    return est.flow_rate / area if area > 0 else 0.0
