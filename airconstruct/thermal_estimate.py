"""Instant lumped thermal estimate (no CFD): one temperature per component.

Owned by: core pipeline. Used to initialise the CFD solid temperatures (the partitioned
conjugate coupling converges slowly, so a good first guess saves thousands of
iterations) and as an instant preview in the UI.

Model: each component is a node. Touching components exchange heat by conduction
through their contact area; exposed faces exchange heat with the air through a forced
convection coefficient (laminar flat plate, Nu = 0.664 Re^0.5 Pr^1/3, at least 5 W/m2/K
for natural convection). The air is at the inlet temperature plus half of its total
heating (mean of inlet and outlet).

Limitation: every component is isothermal, so low-conductivity plates (FR4 boards) are
treated as perfect heat spreaders and the estimate is optimistic for parts sitting on
them. Good enough as a CFD starting point and a rough preview, not as a result.
"""

from __future__ import annotations

import numpy as np

from .case import Case, Component, OpeningKind
from .fans import estimate_flow
from .materials import GASES, SOLIDS
from .mesh import FACE_AXES

TOL = 1e-9


def _overlap(a: Component, b: Component) -> tuple[float, int, float, float] | None:
    """Contact between two boxes: (area, axis, thickness_a, thickness_b) or None."""
    amin, amax, bmin, bmax = a.box.min, a.box.max, b.box.min, b.box.max
    for ax in range(3):
        if abs(amax[ax] - bmin[ax]) < TOL or abs(bmax[ax] - amin[ax]) < TOL:
            o = [ax2 for ax2 in range(3) if ax2 != ax]
            area = 1.0
            for k in o:
                area *= max(0.0, min(amax[k], bmax[k]) - max(amin[k], bmin[k]))
            if area > 0:
                return area, ax, a.box.size.as_tuple()[ax], b.box.size.as_tuple()[ax]
    return None


def _surface(c: Component) -> float:
    x, y, z = c.box.size.as_tuple()
    return 2 * (x * y + y * z + x * z)


def air_state(case: Case) -> tuple[float, float, float]:
    """(mean air temperature degC, characteristic velocity m/s, mass flow kg/s)."""
    gas = GASES[case.fluid]
    t_in = case.ambient_temperature
    rho = case.ambient_pressure * gas.mol_weight * 1e-3 / (8.314 * (t_in + 273.15))
    q = estimate_flow(case).flow_rate
    a_cross = 1.0
    E = case.enclosure.as_tuple()
    for o in case.openings:
        if o.kind == OpeningKind.inlet:
            _, _, (a, b) = FACE_AXES[o.face]
            a_cross = E[a] * E[b]
    mdot = rho * q
    power = sum(c.power for c in case.components)
    t_air = t_in + (0.5 * power / (mdot * gas.cp) if mdot > 0 else 0.0)
    return t_air, max(q / a_cross, 0.05), mdot


def estimate_temperatures(case: Case) -> dict[str, float]:
    """Steady temperature [degC] per component."""
    comps = case.components
    n = len(comps)
    if n == 0:
        return {}
    gas = GASES[case.fluid]
    t_air, u, _ = air_state(case)
    rho = case.ambient_pressure * gas.mol_weight * 1e-3 / (8.314 * (t_air + 273.15))
    nu = gas.mu / rho
    k_air = gas.mu * gas.cp / gas.pr
    G = np.zeros((n, n))
    rhs = np.array([c.power for c in comps], dtype=float)
    exposed = [_surface(c) for c in comps]
    for i in range(n):
        for j in range(i + 1, n):
            ov = _overlap(comps[i], comps[j])
            if not ov:
                continue
            area, _, ta, tb = ov
            ka, kb = SOLIDS[comps[i].material].kappa, SOLIDS[comps[j].material].kappa
            g = area / (ta / (2 * ka) + tb / (2 * kb))
            G[i, i] += g
            G[j, j] += g
            G[i, j] -= g
            G[j, i] -= g
            exposed[i] -= area
            exposed[j] -= area
    for i, c in enumerate(comps):
        L = max(np.sqrt(c.box.size.x * c.box.size.y), 1e-3)
        re = u * L / nu
        h = max(5.0, 0.664 * re**0.5 * gas.pr ** (1 / 3) * k_air / L)
        g = h * max(exposed[i], 0.05 * _surface(c))
        G[i, i] += g
        rhs[i] += g * t_air
    t = np.linalg.solve(G, rhs)
    return {c.name: float(v) for c, v in zip(comps, t, strict=True)}
