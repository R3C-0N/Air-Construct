"""Validation checks: compare a finished run with analytic or reference values.

Owned by: workstream "validation".

Two kinds of checks:

- Conservation checks, valid for any case with adiabatic enclosure walls:
  the outlet mass flow equals the inlet one, and the heat carried away by the air,
  m_dot * cp * (T_out - T_in), equals the dissipated power. These are exact at
  steady state, so their tolerance is tight.
- Physics checks for the "heated blocks in a channel" case
  (validation/heated_blocks/): mirror-symmetric blocks have the same temperature,
  the block in the wake of another one runs hotter, and each block's convection
  coefficient h = P / (A_wet * (T_block - T_air)) lies in a band around the laminar
  flat-plate correlation Nu = 0.664 Re^0.5 Pr^(1/3) (an obstacle's leading edges and
  the channel acceleration raise h above the flat-plate value, never below about half).

``run_checks`` returns plain :class:`Check` objects; ``format_report`` renders them as
a Markdown table (written next to the results by the CI job).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .case import Case, Component, OpeningKind
from .materials import GASES

R_GAS = 8.314462618  # [J/mol/K]
KELVIN = 273.15


@dataclass
class Check:
    name: str
    value: float
    reference: float
    tolerance: float  # allowed |value - reference| (absolute, in `unit`)
    unit: str = ""
    note: str = ""

    @property
    def error(self) -> float:
        return self.value - self.reference

    @property
    def ok(self) -> bool:
        return math.isfinite(self.value) and abs(self.error) <= self.tolerance


def inlet_mass_flow(case: Case) -> float:
    """Imposed mass flow through the inlets [kg/s], ideal gas at ambient pressure and
    inlet T. Free vents and fans with a curve have no imposed flow and count as 0."""
    gas = GASES[case.fluid]
    total = 0.0
    for o in case.openings:
        if o.kind != OpeningKind.inlet or not o.fixed_flow:
            continue
        t = o.temperature if o.temperature is not None else case.ambient_temperature
        rho = case.ambient_pressure * gas.mol_weight * 1e-3 / (R_GAS * (t + KELVIN))
        total += rho * o.normal_velocity() * o.area()
    return total


def wetted_area(case: Case, c: Component) -> float:
    """Area of the faces of c that touch air: faces lying on the enclosure walls
    are not counted (contact with other components is ignored)."""
    lo, hi, E = c.box.min, c.box.max, case.enclosure.as_tuple()
    s = c.box.size.as_tuple()
    area = 0.0
    for ax in range(3):
        a, b = [s[i] for i in range(3) if i != ax]
        area += (lo[ax] > 1e-12) * a * b + (hi[ax] < E[ax] - 1e-12) * a * b
    return area


def flat_plate_h(case: Case, c: Component, u: float, t_film_c: float) -> float:
    """Mean laminar flat-plate convection coefficient over the block length along x."""
    gas = GASES[case.fluid]
    t = t_film_c + KELVIN
    rho = case.ambient_pressure * gas.mol_weight * 1e-3 / (R_GAS * t)
    mu = gas.mu * (t / 293.15) ** 0.7  # Sutherland-like power law for air
    k = mu * gas.cp / gas.pr
    L = c.box.size.x
    re = rho * u * L / mu
    nu = 0.664 * math.sqrt(re) * gas.pr ** (1 / 3)
    return nu * k / L


def conservation_checks(case: Case, summary, mass_tol: float = 0.005,
                        energy_tol: float = 0.02) -> list[Check]:
    """Mass and energy balance (relative tolerances)."""
    imb = getattr(summary, "mass_imbalance", float("nan"))
    checks = [Check("mass balance: outlet / inlet mass flow", 1.0 + imb, 1.0, mass_tol)]
    inlets = [o for o in case.openings if o.kind == OpeningKind.inlet]
    if all(o.fixed_flow for o in inlets):
        m_in = inlet_mass_flow(case)
        checks.append(Check("inlet mass flow / rho U A", summary.mass_flow_kg_s / m_in, 1.0,
                            mass_tol, "", f"rho U A = {m_in * 1e3:.4f} g/s"))
    else:  # the flow rate is a result (fan curve, free vents)
        m_in = summary.mass_flow_kg_s
    power = summary.total_power_w
    if power > 0:
        checks.append(Check("energy balance: heat to air / power", summary.heat_to_air_w / power,
                            1.0, energy_tol, "", f"{power:.2f} W dissipated"))
        gas = GASES[case.fluid]
        rise = power / (m_in * gas.cp)
        t_in = summary.inlet_t_mean_c
        checks.append(Check("outlet temperature rise P/(m_dot cp)",
                            summary.outlet_t_mean_c - t_in, rise, energy_tol * rise, "K"))
    return checks


def heated_blocks_checks(case: Case, summary, sym_tol: float = 0.02,
                         h_band: tuple[float, float] = (0.7, 3.0)) -> list[Check]:
    """Physics checks of validation/heated_blocks (blocks block_a, block_b, block_c)."""
    comps = {c.name: c for c in case.components}
    res = {r.name: r for r in summary.components}
    t_in = summary.inlet_t_mean_c
    if not math.isfinite(t_in):
        t_in = case.ambient_temperature
    a, b, c = (res[n] for n in ("block_a", "block_b", "block_c"))
    rise_a = a.t_mean_c - t_in
    checks = [
        Check("symmetry: block_b - block_a", b.t_mean_c - a.t_mean_c, 0.0,
              sym_tol * abs(rise_a), "K", "mirror images about y = 0.05 m"),
        # block_c sits in block_a's heated wake: it must run hotter. Encoded as
        # 'excess temperature ratio - 1 is in (0, 1]'.
        Check("wake: (T_c - T_in) / (T_a - T_in) - 1",
              (c.t_mean_c - t_in) / rise_a - 1.0 if rise_a else float("nan"), 0.5, 0.5, "",
              "downstream block is hotter, by less than 2x"),
    ]
    u = max(o.normal_velocity() for o in case.openings if o.kind == OpeningKind.inlet)
    m_in = inlet_mass_flow(case)
    cp = GASES[case.fluid].cp
    lo_f, hi_f = h_band
    mid = (lo_f + hi_f) / 2
    for name in ("block_a", "block_b", "block_c"):
        comp, r = comps[name], res[name]
        upstream = sum(o.power for o in case.components if o.box.max[0] <= comp.box.min[0] + 1e-12)
        t_air = t_in + upstream / (m_in * cp)
        h_ref = flat_plate_h(case, comp, u, (r.t_mean_c + t_air) / 2)
        h_sim = comp.power / (wetted_area(case, comp) * (r.t_mean_c - t_air))
        checks.append(Check(f"{name}: h / h_flat_plate", h_sim / h_ref, mid, (hi_f - lo_f) / 2, "",
                            f"h = {h_sim:.1f} W/m2K, flat plate {h_ref:.1f} W/m2K"))
    return checks


def run_checks(case: Case, summary) -> list[Check]:
    checks = conservation_checks(case, summary)
    if {"block_a", "block_b", "block_c"} <= {c.name for c in case.components}:
        checks += heated_blocks_checks(case, summary)
    return checks


def format_report(case: Case, summary, checks: list[Check]) -> str:
    lines = [f"# Validation: {case.name}", "",
             "| check | value | reference | tolerance | result | note |",
             "|---|---|---|---|---|---|"]
    for ch in checks:
        u = f" {ch.unit}" if ch.unit else ""
        lines.append(f"| {ch.name} | {ch.value:.4g}{u} | {ch.reference:.4g}{u} | "
                     f"±{ch.tolerance:.3g}{u} | {'PASS' if ch.ok else 'FAIL'} | {ch.note} |")
    lines += ["", "| component | power [W] | T max [°C] | T mean [°C] |", "|---|---|---|---|"]
    for r in summary.components:
        lines.append(f"| {r.name} | {r.power_w:g} | {r.t_max_c:.2f} | {r.t_mean_c:.2f} |")
    lines += ["", (f"Inlet {summary.inlet_t_mean_c:.2f} °C, outlet {summary.outlet_t_mean_c:.2f} °C, "
              f"pressure drop {summary.pressure_drop_pa:.3g} Pa, "
              f"max air speed {summary.air_v_max:.3g} m/s.")]
    return "\n".join(lines) + "\n"


def write_report(case: Case, summary, checks: list[Check], path: str | Path) -> Path:
    path = Path(path)
    path.write_text(format_report(case, summary, checks), encoding="utf-8")
    return path
