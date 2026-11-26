"""Instant lumped thermal estimate (no CFD): one temperature per component.

Owned by: core pipeline. Used to initialise the CFD solid temperatures (the partitioned
conjugate coupling converges slowly, so a good first guess saves thousands of
iterations) and as an instant preview in the UI.

Model: each component is a node. Touching components exchange heat by conduction
through their contact area; exposed faces exchange heat with the air through a forced
convection coefficient (laminar flat plate, Nu = 0.664 Re^0.5 Pr^1/3, at least 5 W/m2/K
for natural convection). The air is one more node (mean of inlet and outlet
temperatures) cooled by the flow and by the enclosure walls that exchange heat.

Limitation: every component is isothermal, so low-conductivity plates (FR4 boards) are
treated as perfect heat spreaders and the estimate is optimistic for parts sitting on
them. Good enough as a CFD starting point and a rough preview, not as a result.
"""

from __future__ import annotations

import numpy as np

from .case import Case, Component, Face, OpeningKind, WallMode
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


def _wall_contact(case: Case, c: Component) -> list[tuple[Face, float]]:
    """(enclosure face, area) of the component faces lying on the enclosure walls."""
    E = case.enclosure.as_tuple()
    lo, hi, s = c.box.min, c.box.max, c.box.size.as_tuple()
    out = []
    for face, (normal, at_max, (a, b)) in FACE_AXES.items():
        on = abs(hi[normal] - E[normal]) < TOL if at_max else abs(lo[normal]) < TOL
        if on:
            out.append((face, s[a] * s[b]))
    return out


def _face_area(case: Case, face: Face) -> float:
    E = case.enclosure.as_tuple()
    _, _, (a, b) = FACE_AXES[face]
    return E[a] * E[b]


H_INSIDE = 5.0  # [W/m2/K] air to the inner wall surface (natural convection)


def estimate_temperatures(case: Case) -> dict[str, float]:
    """Steady temperature [degC] per component.

    Nodes: every component plus the mean air temperature. The air gets the heat the
    components give it and loses it to the flow (2 m_dot cp (T_air - T_in), since the
    mean air temperature is halfway between inlet and outlet) and through the walls
    that exchange heat. Components lying on such a wall also lose heat through it."""
    comps = case.components
    n = len(comps)
    if n == 0:
        return {}
    gas = GASES[case.fluid]
    t_in = case.ambient_temperature
    t_air, u, mdot = air_state(case)
    rho = case.ambient_pressure * gas.mol_weight * 1e-3 / (8.314 * (t_air + 273.15))
    nu = gas.mu / rho
    k_air = gas.mu * gas.cp / gas.pr
    G = np.zeros((n + 1, n + 1))  # last node: air
    rhs = np.zeros(n + 1)
    rhs[:n] = [c.power for c in comps]
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

    def to_fixed(i: int, g: float, t: float) -> None:
        G[i, i] += g
        rhs[i] += g * t

    covered = dict.fromkeys(FACE_AXES, 0.0)  # wall area hidden by components
    for i, c in enumerate(comps):
        for face, area in _wall_contact(case, c):
            exposed[i] -= area
            covered[face] += area
            w = case.walls.for_face(face)
            if w.mode == WallMode.exchange:
                k = SOLIDS[c.material].kappa
                d = c.box.size.as_tuple()[FACE_AXES[face][0]]
                t_ext = w.t_external if w.t_external is not None else t_in
                to_fixed(i, area / (d / (2 * k) + w.resistance()), t_ext)
    for i, c in enumerate(comps):
        L = max(np.sqrt(c.box.size.x * c.box.size.y), 1e-3)
        re = u * L / nu
        h = max(5.0, 0.664 * re**0.5 * gas.pr ** (1 / 3) * k_air / L)
        g = h * max(exposed[i], 0.05 * _surface(c))
        G[i, i] += g
        G[n, n] += g
        G[i, n] -= g
        G[n, i] -= g
    g_flow = 2.0 * mdot * gas.cp
    to_fixed(n, g_flow, t_in)
    for face in FACE_AXES:
        w = case.walls.for_face(face)
        if w.mode == WallMode.exchange:
            area = max(0.0, _face_area(case, face) - covered[face])
            t_ext = w.t_external if w.t_external is not None else t_in
            to_fixed(n, area / (1.0 / H_INSIDE + w.resistance()), t_ext)
    if G[n, n] - sum(-G[n, i] for i in range(n)) <= 0:  # no flow, adiabatic: air at T_in
        G[n, :] = 0.0
        G[n, n] = 1.0
        rhs[n] = t_in
    t = np.linalg.solve(G, rhs)
    return {c.name: float(v) for c, v in zip(comps, t[:n], strict=True)}
