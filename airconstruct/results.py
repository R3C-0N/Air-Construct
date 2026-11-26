"""Post-processing with PyVista: key numbers, images, report.

Owned by: workstream "results".

Reads a chtMultiRegionFoam case (one fluid region "air" plus one solid region per
component, interfaces "<a>_to_<b>") with PyVista's OpenFOAMReader, at the latest time.

Public interface (used by cli and ui):
- load_regions(case_dir) -> {region: UnstructuredGrid}
- summarize(case, case_dir) -> Summary
- render_images(case, case_dir, out_dir) -> [png paths]
- write_report(case, case_dir, out_dir) -> report.html (also writes summary.json)

Conventions: OpenFOAM temperatures are Kelvin, everything returned here is degC.
The pressure drop uses p_rgh (static pressure minus the hydrostatic head) when it is
available, so that inlets and outlets at different heights compare fairly; otherwise p.

2D maps are drawn with matplotlib (no OpenGL needed, works on any CI). 3D views
(streamlines, component surfaces) need an OpenGL context; they are probed in a child
process first, because VTK can crash the whole process when no context exists.
"""

from __future__ import annotations

import base64
import html
import json
import os
import subprocess
import sys
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np

from .case import Case, OpeningKind
from .foam.writer import opening_p0
from .materials import GASES

KELVIN = 273.15
R_GAS = 8.314462618  # [J/mol/K]


@dataclass
class ComponentResult:
    name: str
    power_w: float
    t_max_c: float
    t_mean_c: float
    t_min_c: float = float("nan")


@dataclass
class OpeningResult:
    """Flow through one opening. mass_flow_kg_s > 0 means into the enclosure."""

    name: str
    kind: str  # "inlet" | "outlet"
    area_m2: float
    mass_flow_kg_s: float = float("nan")
    t_mean_c: float = float("nan")  # flux weighted
    velocity_mean: float = float("nan")  # mean normal speed [m/s]
    p_static_pa: float = float("nan")  # area mean of p_rgh (or p), gauge vs ambient
    p_total_pa: float = float("nan")  # flux weighted static + dynamic, gauge vs ambient
    heat_w: float = float("nan")  # enthalpy carried out (> 0) relative to the inlet air


@dataclass
class HeatFlow:
    """Net heat flow [W] from `source` to `target` (always >= 0).

    Nodes: a component name, "air", "walls" (the enclosure walls) or an outlet name.
    kind: "conduction" (solid to solid), "convection" (solid to air), "wall" (to the
    enclosure walls) or "advection" (carried out by the air through an opening)."""

    source: str
    target: str
    watts: float
    kind: str


@dataclass
class PartBalance:
    """Where the heat of one component goes [W]. Positive = leaving the part."""

    name: str
    power_w: float
    to_air_w: float = 0.0
    to_walls_w: float = 0.0
    to_parts_w: dict[str, float] = field(default_factory=dict)

    @property
    def out_w(self) -> float:
        return self.to_air_w + self.to_walls_w + sum(self.to_parts_w.values())

    @property
    def residual_w(self) -> float:
        """power - heat leaving: zero once the part's temperature has converged."""
        return self.power_w - self.out_w


@dataclass
class EnergyBalance:
    """Detailed steady-state energy balance, computed from the solver's own face
    fluxes: conduction k (T_face - T_cell) / d on every solid patch, k_eff (T_face -
    T_cell) / d on the air walls, m_dot cp (T - T_in) through the openings."""

    parts: list[PartBalance] = field(default_factory=list)
    air_to_openings_w: dict[str, float] = field(default_factory=dict)
    air_to_walls_w: float = 0.0
    total_power_w: float = 0.0
    flows: list[HeatFlow] = field(default_factory=list)

    @property
    def heat_out_w(self) -> float:
        """Heat leaving the enclosure: through the openings and through the walls."""
        return (sum(self.air_to_openings_w.values()) + self.air_to_walls_w
                + sum(p.to_walls_w for p in self.parts))

    @property
    def residual_w(self) -> float:
        """Dissipated power not accounted for (not converged yet when large)."""
        return self.total_power_w - self.heat_out_w

    @property
    def error(self) -> float:
        return self.residual_w / self.total_power_w if self.total_power_w > 0 else float("nan")

    def to_sankey(self, min_w: float | None = None) -> dict:
        """{"nodes": [{"id", "label", "kind", "power_w"}], "links": [{"source", "target",
        "value", "kind"}]}, ready for a Sankey chart (d3-sankey, ECharts, Plotly).

        Node kinds: "part", "air", "walls", "opening", "residual". Links are net flows per
        pair of nodes [W]; flows under min_w (default 0.1 % of the power) are left out. A
        "residual" link closes the balance while the run is not fully converged."""
        if min_w is None:
            min_w = 1e-3 * self.total_power_w
        links = [{"source": f.source, "target": f.target, "value": f.watts, "kind": f.kind}
                 for f in self.flows if f.watts > min_w]
        res = self.residual_w
        if abs(res) > max(min_w, 1e-9):
            src, dst = ("air", "residual") if res > 0 else ("residual", "air")
            links.append({"source": src, "target": dst, "value": abs(res), "kind": "residual"})
        power = {p.name: p.power_w for p in self.parts}
        kinds = {"air": "air", "walls": "walls", "residual": "residual"}
        kinds.update({n: "opening" for n in self.air_to_openings_w})
        kinds.update({n: "part" for n in power})
        used = dict.fromkeys(n for link in links for n in (link["source"], link["target"]))
        nodes = [{"id": n, "label": n, "kind": kinds.get(n, "part"), "power_w": power.get(n, 0.0)}
                 for n in used]
        return {"nodes": nodes, "links": links}


@dataclass
class FanResult:
    """Operating point of a fan: the flow it delivers and the pressure rise it gives.

    For a fan with a curve, pressure_rise_pa is the curve read at the simulated flow
    rate (the solver keeps the fan on its curve). For a fan with an imposed flow rate,
    it is the static pressure the system needs: mean p_rgh on the fan minus ambient
    for an intake fan, ambient minus it for an exhaust fan."""

    name: str
    flow_rate_m3_s: float
    pressure_rise_pa: float
    direction: str  # "in" or "out"


@dataclass
class Summary:
    components: list[ComponentResult]
    air_t_max_c: float
    air_v_max: float  # [m/s]
    pressure_drop_pa: float  # mean inlet pressure - mean outlet pressure
    outlet_t_mean_c: float
    # extras (keyword defaults keep the original constructor working)
    time: float = 0.0  # time (iteration for steady runs) the numbers come from
    inlet_t_mean_c: float = float("nan")
    air_t_mean_c: float = float("nan")  # volume weighted
    mass_flow_kg_s: float = float("nan")  # through the inlets (outlets if no inlet data)
    mass_imbalance: float = float("nan")  # (outlet - inlet) / inlet mass flow
    heat_to_air_w: float = float("nan")  # mass_flow * cp * (T_out - T_in)
    total_power_w: float = 0.0
    energy_balance_error: float = float("nan")  # (heat_to_air + walls - power) / power
    pressure_field: str = "p_rgh"
    warnings: list[str] = field(default_factory=list)
    fans: list[FanResult] = field(default_factory=list)
    pressure_drop_total_pa: float = float("nan")  # inlet - outlet total pressure
    openings: list[OpeningResult] = field(default_factory=list)
    energy: EnergyBalance | None = None
    heat_through_walls_w: float = 0.0  # leaving through the enclosure walls (0 if adiabatic)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.energy is not None:  # derived numbers and the Sankey links, for the UI
            e = d["energy"]
            e.update(heat_out_w=self.energy.heat_out_w, residual_w=self.energy.residual_w,
                     error=self.energy.error, sankey=self.energy.to_sankey())
            for pd, p in zip(e["parts"], self.energy.parts):
                pd.update(out_w=p.out_w, residual_w=p.residual_w)
        return d

    def hottest(self) -> ComponentResult | None:
        valid = [c for c in self.components if np.isfinite(c.t_max_c)]
        return max(valid, key=lambda c: c.t_max_c, default=None)


# --------------------------------------------------------------------------- reading


def _foam_file(case_dir: Path) -> Path:
    case_dir = Path(case_dir)
    existing = sorted(case_dir.glob("*.foam"))
    if existing:
        return existing[0]
    f = case_dir / "case.foam"
    f.touch()
    return f


def _reader(case_dir: Path, time: float | None = None):
    import pyvista as pv

    case_dir = Path(case_dir)
    if not (case_dir / "constant").is_dir():
        raise FileNotFoundError(f"{case_dir} is not an OpenFOAM case (no constant/)")
    r = pv.OpenFOAMReader(str(_foam_file(case_dir)))
    if not _has_time_dirs(case_dir) and (case_dir / "processor0").is_dir():
        r.case_type = "decomposed"
    times = list(r.time_values)
    if not times:
        raise FileNotFoundError(f"no time directory with results in {case_dir}")
    r.set_active_time_value(times[-1] if time is None else min(times, key=lambda t: abs(t - time)))
    r.enable_all_patch_arrays()
    names = list(r.patch_array_names)
    if any(n.startswith("/") for n in names):
        # multi-region case: skip the parent mesh left in constant/polyMesh by
        # splitMeshRegions (VTK calls it "defaultRegion"); it carries no results
        for n in names:
            if not n.startswith("/"):
                r.disable_patch_array(n)
    r.enable_all_cell_arrays()
    r.cell_to_point_creation = True
    return r


def _has_time_dirs(case_dir: Path) -> bool:
    for d in case_dir.iterdir():
        try:
            float(d.name)
        except ValueError:
            continue
        if d.is_dir():
            return True
    return False


def _read_blocks(case_dir: Path, time: float | None = None) -> tuple[float, dict[str, dict]]:
    """(time, {region: {"internal": grid, "patches": {name: PolyData}}})."""
    r = _reader(case_dir, time)
    mb = r.read()
    out: dict[str, dict] = {}
    keys = list(mb.keys())
    if "internalMesh" in keys:  # single region case: call it "air"
        mb = {"air": mb}
        keys = ["air"]
    for reg in keys:
        blk = mb[reg]
        names = [] if blk is None else list(blk.keys())
        if reg == "defaultRegion" or "internalMesh" not in names:
            continue
        patches = {}
        b = blk["boundary"] if "boundary" in names else None
        if b is not None:
            for name in list(b.keys()):
                if b[name] is not None and b[name].n_cells:
                    patches[name] = b[name]
        out[reg] = {"internal": blk["internalMesh"], "patches": patches}
    return float(r.active_time_value), out


def load_regions(case_dir: Path, time: float | None = None) -> dict:
    """Return {region_name: pyvista.UnstructuredGrid} for the latest time
    (or the stored time closest to `time`)."""
    _, blocks = _read_blocks(case_dir, time)
    return {reg: b["internal"] for reg, b in blocks.items()}


def load_patches(case_dir: Path, region: str = "air", time: float | None = None) -> dict:
    """Return {patch_name: pyvista.PolyData} of one region (face values as cell data)."""
    _, blocks = _read_blocks(case_dir, time)
    return blocks[region]["patches"]


# --------------------------------------------------------------------------- numbers


def _volumes(grid) -> np.ndarray:
    return np.abs(grid.compute_cell_sizes(length=False, area=False, volume=True)["Volume"])


def _areas(poly) -> np.ndarray:
    return np.abs(poly.compute_cell_sizes(length=False, area=True, volume=False)["Area"])


def _cell(grid, name: str) -> np.ndarray | None:
    return np.asarray(grid.cell_data[name]) if name in grid.cell_data else None


def _wmean(v: np.ndarray, w: np.ndarray) -> float:
    s = float(np.sum(w))
    return float(np.sum(v * w) / s) if s > 0 else float(np.mean(v))


def _patch_stats(patches: dict, names: list[str], fld: str, weights: str = "area"):
    """Weighted mean of `fld` over the given patches (None if no data)."""
    vals, ws = [], []
    for n in names:
        poly = patches.get(n)
        if poly is None or fld not in poly.cell_data:
            continue
        a = _areas(poly)
        w = a
        if weights == "flux" and "U" in poly.cell_data:
            nrm = poly.compute_normals(cell_normals=True, point_normals=False,
                                       auto_orient_normals=False, consistent_normals=False)
            un = np.abs(np.einsum("ij,ij->i", np.asarray(poly.cell_data["U"]),
                                  np.asarray(nrm.cell_data["Normals"])))
            if np.sum(un * a) > 0:
                w = un * a
        vals.append(np.asarray(poly.cell_data[fld]))
        ws.append(w)
    if not vals:
        return None
    return _wmean(np.concatenate(vals), np.concatenate(ws))


def _time_dir(case_dir: Path, region: str, t: float) -> Path | None:
    best, err = None, float("inf")
    for d in Path(case_dir).iterdir():
        try:
            v = float(d.name)
        except ValueError:
            continue
        if (d / region).is_dir() and abs(v - t) < err:
            best, err = d / region, abs(v - t)
    return best


_NUM = r"[-+0-9.eE]+"


def _patch_values(field_file: Path, patch: str) -> np.ndarray | float | None:
    """Face values of `patch` in an ASCII OpenFOAM field file: an array for a nonuniform
    `value`, a float for a uniform scalar one, None when not found."""
    import re

    try:
        txt = field_file.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"\n\s*" + re.escape(patch) + r"\s*\n\s*\{", txt)
    if not m:
        return None
    depth, i = 0, m.end() - 1
    for j in range(i, len(txt)):
        depth += {"{": 1, "}": -1}.get(txt[j], 0)
        if depth == 0:
            body = txt[i:j]
            break
    else:
        return None
    v = re.search(r"\bvalue\s+nonuniform\s+List<scalar>\s*(\d+)\s*\((.*?)\)\s*;", body, re.DOTALL)
    if v:
        arr = np.array(v.group(2).split(), dtype=float)
        return arr if len(arr) == int(v.group(1)) else None
    v = re.search(r"\bvalue\s+uniform\s+(" + _NUM + r")\s*;", body)
    return float(v.group(1)) if v else None


def _flux_stats(case_dir: Path, t: float, names: list[str], region: str = "air"):
    """(mass flow [kg/s], flux-weighted T [K]) through the given patches, from the
    solver's face mass fluxes (phi) and face temperatures. Exact, unlike values
    rebuilt from the boundary velocity. None when phi is not available."""
    d = _time_dir(case_dir, region, t)
    if d is None:
        return None
    mdot, ht = 0.0, 0.0
    for n in names:
        phi = _patch_values(d / "phi", n)
        T = _patch_values(d / "T", n)
        if not isinstance(phi, np.ndarray) or T is None:
            return None
        T = np.full(len(phi), T) if isinstance(T, float) else T
        if len(T) != len(phi):
            return None
        mdot += float(np.sum(phi))
        ht += float(np.sum(phi * T))
    if not names or mdot == 0:
        return None
    return abs(mdot), ht / mdot


def _mass_flow(patches: dict, names: list[str], molw: float) -> float | None:
    """Mass flow [kg/s] through the given patches (magnitude, either direction)."""
    total, found = 0.0, False
    for n in names:
        poly = patches.get(n)
        if poly is None or "U" not in poly.cell_data:
            continue
        found = True
        nrm = poly.compute_normals(cell_normals=True, point_normals=False,
                                   auto_orient_normals=False, consistent_normals=False)
        un = np.einsum("ij,ij->i", np.asarray(poly.cell_data["U"]),
                       np.asarray(nrm.cell_data["Normals"]))
        un = np.abs(un)  # face normals of a boundary patch point outwards
        if "rho" in poly.cell_data:
            rho = np.asarray(poly.cell_data["rho"])
        elif "p" in poly.cell_data and "T" in poly.cell_data:
            rho = np.asarray(poly.cell_data["p"]) * molw * 1e-3 / (
                R_GAS * np.asarray(poly.cell_data["T"]))
        else:
            rho = np.full(poly.n_cells, 1.18)
        total += float(np.sum(rho * un * _areas(poly)))
    return total if found else None


SIGMA = 5.670374419e-8  # Stefan-Boltzmann [W/m2/K4]


def wall_heat_loss(case: Case, blocks: dict) -> float:
    """Heat [W] leaving through the enclosure walls, from the inner wall temperature of
    every region (air and solids touching the walls). Same law as OpenFOAM's
    externalWallHeatFluxTemperature in coefficient mode:
    q = (T_w - T_a) / (1 / (h + h_rad) + t / kappa), h_rad = eps sigma (T_w^2 + T_a^2)(T_w + T_a)."""
    from .case import WallMode
    from .materials import SOLIDS

    total = 0.0
    for name, faces in case.wall_patches().items():
        w = case.walls.for_face(faces[0])
        if w.mode != WallMode.exchange:
            continue
        ta = (w.t_external if w.t_external is not None else case.ambient_temperature) + KELVIN
        r_wall = w.thickness / SOLIDS[w.material].kappa
        for b in blocks.values():
            poly = b["patches"].get(name)
            if poly is None or "T" not in poly.cell_data:
                continue
            tw = np.asarray(poly.cell_data["T"], dtype=float)
            h = w.h_external + w.emissivity * SIGMA * (tw**2 + ta**2) * (tw + ta)
            total += float(np.sum(_areas(poly) * (tw - ta) / (1.0 / h + r_wall)))
    return total


def summarize(case: Case, case_dir: Path, time: float | None = None) -> Summary:
    t, blocks = _read_blocks(case_dir, time)
    warnings: list[str] = []
    if "air" not in blocks:
        raise ValueError(f"no 'air' region in {case_dir} (found {sorted(blocks)})")
    air = blocks["air"]["internal"]
    patches = blocks["air"]["patches"]

    comps = []
    for c in case.components:
        b = blocks.get(c.name)
        T = _cell(b["internal"], "T") if b else None
        if T is None or len(T) == 0:
            warnings.append(f"component {c.name!r}: no temperature found (not meshed?)")
            comps.append(ComponentResult(c.name, c.power, float("nan"), float("nan")))
            continue
        comps.append(ComponentResult(
            c.name, c.power, float(T.max()) - KELVIN,
            _wmean(T, _volumes(b["internal"])) - KELVIN, float(T.min()) - KELVIN))

    Ta = _cell(air, "T")
    U = _cell(air, "U")
    if Ta is None:
        raise ValueError("air region has no T field")
    air_t_max = float(Ta.max()) - KELVIN
    air_t_mean = _wmean(Ta, _volumes(air)) - KELVIN
    v_max = float(np.linalg.norm(U, axis=1).max()) if U is not None else float("nan")

    inlets = [o.name for o in case.openings if o.kind == OpeningKind.inlet]
    outlets = [o.name for o in case.openings if o.kind == OpeningKind.outlet]
    for n in inlets + outlets:
        if n not in patches:
            warnings.append(f"opening {n!r}: no patch of that name in the air region")

    pfield = "p_rgh" if any("p_rgh" in patches[n].cell_data for n in inlets if n in patches) else "p"
    p_in = _patch_stats(patches, inlets, pfield)
    p_out = _patch_stats(patches, outlets, pfield)
    dp = (p_in - p_out) if p_in is not None and p_out is not None else float("nan")

    T_out = _patch_stats(patches, outlets, "T", weights="flux")
    T_in = _patch_stats(patches, inlets, "T", weights="flux")
    t_out_c = T_out - KELVIN if T_out is not None else float("nan")
    t_in_c = T_in - KELVIN if T_in is not None else float("nan")

    gas = GASES[case.fluid]
    m_in = _mass_flow(patches, inlets, gas.mol_weight)
    m_out = _mass_flow(patches, outlets, gas.mol_weight)
    # prefer the solver's own face fluxes: an outlet's boundary velocity is not its face
    # flux (tangential and back-flow parts), which overestimated the outlet mass flow by
    # ~20 % and underestimated the outlet temperature by as much on validation/heated_blocks
    f_in = _flux_stats(case_dir, t, inlets)
    f_out = _flux_stats(case_dir, t, outlets)
    if f_in is not None and f_out is not None:
        m_in, m_out = f_in[0], f_out[0]
        t_in_c, t_out_c = f_in[1] - KELVIN, f_out[1] - KELVIN
    imbalance = float("nan")
    if m_in and m_out:
        imbalance = (m_out - m_in) / m_in
        if abs(imbalance) > 0.02:
            warnings.append(
                f"mass balance off by {imbalance:+.1%}: {m_in * 1000:.3f} g/s in, "
                f"{m_out * 1000:.3f} g/s out (the flow is not fully converged)")
    # the imposed inlet flow is the most accurate figure, use it for the heat balance
    mdot = m_in if m_in is not None else m_out
    mdot = mdot if mdot is not None else float("nan")
    q_air = mdot * gas.cp * (t_out_c - t_in_c)
    power = float(sum(c.power for c in case.components))
    q_walls = wall_heat_loss(case, blocks)
    err = (q_air + q_walls - power) / power if power > 0 else float("nan")
    if power > 0 and np.isfinite(err) and abs(err) > 0.2:
        warnings.append(
            f"energy balance off by {err:+.0%}: air carries {q_air:.2f} W and the walls "
            f"{q_walls:.2f} W for {power:.2f} W dissipated (not converged yet?)")

    fans = []
    for o in case.openings:
        if o.fan is None:
            continue
        fr = _flux_stats(case_dir, t, [o.name])
        if fr is not None:
            rho = case.ambient_pressure * gas.mol_weight * 1e-3 / (R_GAS * fr[1])
            q = fr[0] / rho
        else:  # no face fluxes written: rebuild the flow from the boundary velocity
            m = _mass_flow(patches, [o.name], gas.mol_weight)
            t_f = _patch_stats(patches, [o.name], "T")
            if m is not None and t_f is not None:
                q = m * R_GAS * t_f / (case.ambient_pressure * gas.mol_weight * 1e-3)
            else:
                q = float("nan")
        if o.fan.curve is not None:
            dp = o.fan.pressure_at(q) if np.isfinite(q) else float("nan")
        else:
            p_fan = _patch_stats(patches, [o.name], pfield)
            p_amb = opening_p0(case, o) if pfield == "p_rgh" else case.ambient_pressure
            if p_fan is None:
                dp = float("nan")
            else:
                dp = p_fan - p_amb if o.kind == OpeningKind.inlet else p_amb - p_fan
        fans.append(FanResult(o.name, q, dp, "in" if o.kind == OpeningKind.inlet else "out"))
    t_ref = (t_in_c if np.isfinite(t_in_c) else case.ambient_temperature) + KELVIN
    openings = _openings(case, case_dir, t, patches, pfield, t_ref)
    dp_total = float("nan")
    pt = {k: [(o.p_total_pa, abs(o.mass_flow_kg_s)) for o in openings
              if o.kind == k and np.isfinite(o.p_total_pa)] for k in ("inlet", "outlet")}
    if pt["inlet"] and pt["outlet"]:
        def mean(v):
            w = np.array([b for _, b in v])
            return _wmean(np.array([a for a, _ in v]), w if w.sum() > 0 else np.ones(len(v)))
        dp_total = mean(pt["inlet"]) - mean(pt["outlet"])
    try:
        energy = energy_balance(case, case_dir, blocks, t, openings)
    except (ValueError, KeyError, IndexError) as e:  # unexpected mesh layout
        energy = None
        warnings.append(f"detailed energy balance not available: {e}")
    if energy is None and not any("energy balance not" in w for w in warnings):
        warnings.append("detailed energy balance not available (mesh files not readable)")
    elif energy is not None and power > 0:
        for part in energy.parts:
            if abs(part.residual_w) > max(0.05 * part.power_w, 0.02 * power):
                warnings.append(
                    f"component {part.name!r}: {part.out_w:.2f} W leave it for {part.power_w:.2f} W "
                    "dissipated (its temperature has not converged)")

    if energy is not None and any(
            case.walls.for_face(f[0]).mode == "exchange" for f in case.wall_patches().values()):
        # the solver's own wall fluxes, consistent with the per-part balance
        q_walls = energy.air_to_walls_w + sum(p.to_walls_w for p in energy.parts)
        err = (q_air + q_walls - power) / power if power > 0 else float("nan")

    return Summary(
        fans=fans, heat_through_walls_w=q_walls,
        components=comps, air_t_max_c=air_t_max, air_v_max=v_max, pressure_drop_pa=dp,
        outlet_t_mean_c=t_out_c, time=t, inlet_t_mean_c=t_in_c, air_t_mean_c=air_t_mean,
        mass_flow_kg_s=mdot, mass_imbalance=imbalance, heat_to_air_w=q_air, total_power_w=power,
        energy_balance_error=err, pressure_field=pfield, warnings=warnings,
        pressure_drop_total_pa=dp_total, openings=openings, energy=energy)


# --------------------------------------------------------------------------- balance


def _foam_list(path: Path) -> np.ndarray | None:
    """Integer list of an ASCII OpenFOAM file (polyMesh/owner), None if unreadable."""
    import gzip
    import re

    if not path.exists() and path.with_suffix(".gz").exists():
        path = path.with_suffix(".gz")
    try:
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    except OSError:
        return None
    if b"format      binary" in raw[:2000] or re.search(rb"format\s+binary", raw[:2000]):
        return None
    txt = raw.decode(errors="replace")
    head = txt.find("}", txt.find("FoamFile"))
    m = re.compile(r"(\d+)\s*\(").search(txt, head + 1)
    if not m:
        return None
    end = txt.find(")", m.end())
    arr = np.array(txt[m.end():end].split(), dtype=np.int64)
    return arr if len(arr) == int(m.group(1)) else None


def _patch_ranges(boundary: Path) -> dict[str, tuple[int, int]]:
    """{patch: (startFace, nFaces)} from a polyMesh/boundary file."""
    import re

    try:
        txt = boundary.read_text(errors="replace")
    except OSError:
        return {}
    txt = txt[txt.find("}", txt.find("FoamFile")) + 1:]
    out = {}
    for m in re.finditer(r"([A-Za-z_][\w.:-]*)\s*\{([^{}]*)\}", txt):
        nf = re.search(r"\bnFaces\s+(\d+)", m.group(2))
        sf = re.search(r"\bstartFace\s+(\d+)", m.group(2))
        if nf and sf:
            out[m.group(1)] = (int(sf.group(1)), int(nf.group(1)))
    return out


def _solid_kappa(case: Case, case_dir: Path, region: str) -> float:
    """Conductivity of a solid region: from its thermophysicalProperties when written
    (what the solver used), else from the material library."""
    import re

    from .materials import SOLIDS

    f = Path(case_dir) / "constant" / region / "thermophysicalProperties"
    try:
        m = re.search(r"\bkappa\s+(" + _NUM + r")\s*;", f.read_text(errors="replace"))
        if m:
            return float(m.group(1))
    except OSError:
        pass
    comp = next((c for c in case.components if c.name == region), None)
    return SOLIDS[comp.material].kappa if comp else float("nan")


def _normals(poly) -> np.ndarray:
    """Face normals of a boundary patch (OpenFOAM orders boundary face points so that
    the normal points out of the region)."""
    n = poly.compute_normals(cell_normals=True, point_normals=False, auto_orient_normals=False,
                             consistent_normals=False, split_vertices=False)
    return np.asarray(n.cell_data["Normals"])


def _wall_fluxes(case_dir: Path, region: str, block: dict, kappa) -> dict[str, float] | None:
    """{patch: conducted heat INTO the region [W]} = sum k (T_face - T_cell) / d A, the
    discretisation the solver itself uses on an orthogonal mesh. kappa(poly) gives the
    face conductivity. None when the mesh files cannot be matched with the reader."""
    mesh = Path(case_dir) / "constant" / region / "polyMesh"
    owner = _foam_list(mesh / "owner")
    ranges = _patch_ranges(mesh / "boundary")
    grid = block["internal"]
    T = _cell(grid, "T")
    if owner is None or T is None or not ranges or int(owner.max()) + 1 != grid.n_cells:
        return None
    centres = np.asarray(grid.cell_centers().points)
    out = {}
    for name, poly in block["patches"].items():
        if name not in ranges or "T" not in poly.cell_data:
            continue
        start, n = ranges[name]
        if n != poly.n_cells:
            return None
        own = owner[start:start + n]
        fc = np.asarray(poly.cell_centers().points)
        d = np.abs(np.einsum("ij,ij->i", fc - centres[own], _normals(poly)))
        d = np.where(d > 0, d, np.nan)
        q = kappa(poly) * (np.asarray(poly.cell_data["T"]) - T[own]) / d * _areas(poly)
        out[name] = float(np.nansum(q))
    return out


def _face_flux(case_dir: Path, t: float, name: str, poly, rho_default: float) -> np.ndarray:
    """Outward face mass flux [kg/s] of an air patch: the solver's phi when written,
    else rho U.n A."""
    d = _time_dir(case_dir, "air", t)
    phi = _patch_values(d / "phi", name) if d is not None else None
    if isinstance(phi, np.ndarray) and len(phi) == poly.n_cells:
        return phi
    un = np.einsum("ij,ij->i", np.asarray(poly.cell_data["U"]), _normals(poly))
    rho = np.asarray(poly.cell_data["rho"]) if "rho" in poly.cell_data else rho_default
    return rho * un * _areas(poly)


def _openings(case: Case, case_dir: Path, t: float, patches: dict, pfield: str,
              t_ref_k: float) -> list[OpeningResult]:
    gas = GASES[case.fluid]
    rho0 = case.ambient_pressure * gas.mol_weight * 1e-3 / (R_GAS * (case.ambient_temperature
                                                                     + KELVIN))
    out = []
    for o in case.openings:
        r = OpeningResult(o.name, o.kind.value, o.area())
        poly = patches.get(o.name)
        out.append(r)
        if poly is None or "U" not in poly.cell_data:
            continue
        a = _areas(poly)
        r.area_m2 = float(a.sum())
        phi = _face_flux(case_dir, t, o.name, poly, rho0)
        w = np.abs(phi) if np.sum(np.abs(phi)) > 0 else a
        r.mass_flow_kg_s = -float(np.sum(phi))
        rho = (np.asarray(poly.cell_data["rho"]) if "rho" in poly.cell_data
               else np.full(poly.n_cells, rho0))
        r.velocity_mean = float(np.sum(np.abs(phi) / rho) / r.area_m2) if r.area_m2 else np.nan
        if "T" in poly.cell_data:
            T = np.asarray(poly.cell_data["T"])
            r.t_mean_c = _wmean(T, w) - KELVIN
            r.heat_w = float(np.sum(phi * gas.cp * (T - t_ref_k)))
        if pfield in poly.cell_data:
            ps = np.asarray(poly.cell_data[pfield]) - case.ambient_pressure
            u2 = np.sum(np.asarray(poly.cell_data["U"]) ** 2, axis=1)
            r.p_static_pa = _wmean(ps, a)
            r.p_total_pa = _wmean(ps + 0.5 * rho * u2, w)
    return out


def energy_balance(case: Case, case_dir: Path, blocks: dict, t: float,
                   openings: list[OpeningResult]) -> EnergyBalance | None:
    """Detailed energy balance (see :class:`EnergyBalance`); None when the mesh files
    cannot be read (binary or decomposed-only cases)."""
    gas = GASES[case.fluid]
    names = {c.name for c in case.components}
    bal = EnergyBalance(total_power_w=float(sum(c.power for c in case.components)))
    air = blocks.get(case.fluid_region)
    q_air: dict[str, float] = {}
    if air is not None:

        def k_air(poly):
            alphat = np.asarray(poly.cell_data["alphat"]) if "alphat" in poly.cell_data else 0.0
            return gas.cp * (gas.mu / gas.pr + alphat)

        q_air = _wall_fluxes(case_dir, case.fluid_region, air, k_air)
        if q_air is None:
            return None
        opening_names = {o.name for o in case.openings}
        for patch, q_in in q_air.items():
            if patch.startswith(f"{case.fluid_region}_to_") or patch in opening_names:
                continue
            bal.air_to_walls_w += -q_in
        for o in openings:
            if np.isfinite(o.heat_w):  # plus conduction across the opening (tiny)
                o.heat_w += -q_air.get(o.name, 0.0)
                bal.air_to_openings_w[o.name] = o.heat_w
    for c in case.components:
        b = blocks.get(c.name)
        part = PartBalance(c.name, c.power)
        bal.parts.append(part)
        if b is None:
            continue
        k = _solid_kappa(case, case_dir, c.name)
        q = _wall_fluxes(case_dir, c.name, b, lambda poly, k=k: k)
        if q is None:
            return None
        prefix = f"{c.name}_to_"
        for patch, q_in in q.items():
            other = patch[len(prefix):] if patch.startswith(prefix) else ""
            if other == case.fluid_region:
                # the air side: the solid side of a highly conductive part lags behind
                # its neighbours in the partitioned coupling (several % on coarse meshes),
                # while the air side adds up exactly to the heat leaving the outlets
                part.to_air_w += q_air.get(f"{case.fluid_region}_to_{c.name}", -q_in)
            elif other in names:
                part.to_parts_w[other] = part.to_parts_w.get(other, 0.0) - q_in
            else:
                part.to_walls_w += -q_in
    # solid to solid: the mean of both sides, so the two parts agree
    by = {p.name: p for p in bal.parts}
    for p in bal.parts:
        for other, w in list(p.to_parts_w.items()):
            back = by[other].to_parts_w.get(p.name) if other in by else None
            if back is not None:
                p.to_parts_w[other] = (w - back) / 2
                by[other].to_parts_w[p.name] = -(w - back) / 2

    # net flow per pair of nodes, in its positive direction
    def add(src: str, dst: str, w: float, kind: str) -> None:
        if not np.isfinite(w) or w == 0:
            return
        src, dst = (src, dst) if w > 0 else (dst, src)
        bal.flows.append(HeatFlow(src, dst, abs(w), kind))

    seen = set()
    for p in bal.parts:
        add(p.name, "air", p.to_air_w, "convection")
        add(p.name, "walls", p.to_walls_w, "wall")
        for other, w in p.to_parts_w.items():
            pair = frozenset((p.name, other))
            if pair not in seen:
                seen.add(pair)
                add(p.name, other, w, "conduction")
    for name, w in bal.air_to_openings_w.items():
        add("air", name, w, "advection")
    add("air", "walls", bal.air_to_walls_w, "wall")
    return bal


# --------------------------------------------------------------------------- images

_AXES = "xyz"


def _focus_point(case: Case) -> np.ndarray:
    """Centre of the most powerful component (enclosure centre if none dissipates)."""
    hot = [c for c in case.components if c.power > 0]
    if hot:
        c = max(hot, key=lambda c: c.power)
        return (np.array(c.box.min) + np.array(c.box.max)) / 2
    return np.array(case.enclosure.as_tuple()) / 2


def _slice_polys(grid, normal_axis: int, value: float, fld: str):
    """Cut `grid` by the plane axis=value. Returns (list of (k,2) polygons, values) or None."""
    if grid.n_cells == 0:
        return None
    normal = [0.0, 0.0, 0.0]
    normal[normal_axis] = 1.0
    origin = list(grid.center)
    origin[normal_axis] = value
    lo, hi = grid.bounds[2 * normal_axis], grid.bounds[2 * normal_axis + 1]
    if not lo <= value <= hi:
        return None
    s = grid.slice(normal=normal, origin=origin)
    if s.n_cells == 0 or fld not in s.cell_data:
        return None
    keep = [a for a in range(3) if a != normal_axis]
    pts = np.asarray(s.points)[:, keep]
    polys = [pts[f] for f in s.irregular_faces]
    vals = np.asarray(s.cell_data[fld])
    return polys, vals


def _plane_map(case: Case, regions: dict, normal_axis: int, value: float, fld: str,
               title: str, label: str, path: Path, air_only: bool = False,
               to_scalar=None, quiver: bool = False, cmap: str = "turbo") -> Path | None:
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from matplotlib.patches import Rectangle

    keep = [a for a in range(3) if a != normal_axis]
    pieces = []
    for reg, grid in regions.items():
        if air_only and reg != "air":
            continue
        cut = _slice_polys(grid, normal_axis, value, fld)
        if cut is not None:
            polys, vals = cut
            vals = to_scalar(vals) if to_scalar else vals
            pieces.append((reg, polys, vals))
    if not pieces:
        return None
    allv = np.concatenate([v for _, _, v in pieces])
    vmin, vmax = float(np.min(allv)), float(np.max(allv))
    if vmax - vmin < 1e-9:
        vmax = vmin + 1e-9

    E = case.enclosure.as_tuple()
    w, h = E[keep[0]], E[keep[1]]
    fig_w = 8.0
    fig, ax = plt.subplots(figsize=(fig_w, max(2.5, min(8.0, fig_w * h / w * 0.9 + 1.2))))
    coll = None
    for _, polys, vals in pieces:
        coll = PolyCollection(polys, array=vals, cmap=cmap, edgecolors="face", linewidths=0.2)
        coll.set_clim(vmin, vmax)
        ax.add_collection(coll)
    for c in case.components:
        lo, hi = c.box.min, c.box.max
        if lo[normal_axis] <= value <= hi[normal_axis]:
            ax.add_patch(Rectangle((lo[keep[0]], lo[keep[1]]), hi[keep[0]] - lo[keep[0]],
                                   hi[keep[1]] - lo[keep[1]], fill=False, ec="k", lw=0.8))
            ax.annotate(c.name, ((lo[keep[0]] + hi[keep[0]]) / 2, hi[keep[1]]),
                        ha="center", va="bottom", fontsize=7)
    if quiver and "air" in regions:
        _add_quiver(ax, regions["air"], normal_axis, value, keep, E)
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.set_aspect("equal")
    ax.set_xlabel(f"{_AXES[keep[0]]} [m]")
    ax.set_ylabel(f"{_AXES[keep[1]]} [m]")
    ax.set_title(f"{title} ({_AXES[normal_axis]} = {value * 1000:.1f} mm)")
    fig.colorbar(coll, ax=ax, label=label, shrink=0.9)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def _add_quiver(ax, air, normal_axis, value, keep, E, n=24):
    """In-plane velocity arrows sampled on a regular grid of the cut plane."""
    import pyvista as pv

    u = np.linspace(0, E[keep[0]], n + 2)[1:-1]
    v = np.linspace(0, E[keep[1]], max(3, int(n * E[keep[1]] / E[keep[0]])) + 2)[1:-1]
    U, V = np.meshgrid(u, v)
    pts = np.zeros((U.size, 3))
    pts[:, keep[0]], pts[:, keep[1]], pts[:, normal_axis] = U.ravel(), V.ravel(), value
    probe = pv.PolyData(pts).sample(air)
    if "U" not in probe.point_data:
        return
    vel = np.asarray(probe.point_data["U"])
    ok = np.asarray(probe.point_data.get("vtkValidPointMask", np.ones(len(pts)))).astype(bool)
    if not ok.any():
        return
    ax.quiver(pts[ok, keep[0]], pts[ok, keep[1]], vel[ok, keep[0]], vel[ok, keep[1]],
              color="black", alpha=0.55, width=0.002)


def _bar_chart(summary: Summary, path: Path) -> Path | None:
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    comps = [c for c in summary.components if np.isfinite(c.t_max_c)]
    if not comps:
        return None
    comps.sort(key=lambda c: c.t_max_c, reverse=True)
    names = [c.name for c in comps]
    y = np.arange(len(comps))
    fig, ax = plt.subplots(figsize=(8, 1.2 + 0.45 * len(comps)))
    ax.barh(y, [c.t_max_c for c in comps], color="#d1495b", label="max")
    ax.barh(y, [c.t_mean_c for c in comps], color="#edae49", height=0.45, label="mean")
    for i, c in enumerate(comps):
        ax.text(c.t_max_c, i, f" {c.t_max_c:.1f} °C", va="center", fontsize=8)
    ax.axvline(summary.inlet_t_mean_c, color="#00798c", ls="--", lw=1, label="inlet air")
    ax.set_yticks(y, names)
    ax.invert_yaxis()
    ax.set_xlabel("Temperature [°C]")
    lo = summary.inlet_t_mean_c if np.isfinite(summary.inlet_t_mean_c) else 0
    ax.set_xlim(min(lo, min(c.t_mean_c for c in comps)) - 2, max(c.t_max_c for c in comps) * 1.08 + 1)
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title("Component temperatures")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


_PROBE = (
    "import pyvista as pv\n"
    "p = pv.Plotter(off_screen=True, window_size=(64, 64))\n"
    "p.add_mesh(pv.Sphere())\n"
    "p.screenshot(return_img=True)\n"
    "p.close()\n"
)


@lru_cache(maxsize=1)
def rendering_available() -> bool:
    """True when VTK can open an off-screen OpenGL context (probed in a child process)."""
    if os.environ.get("AIRCONSTRUCT_NO_3D"):
        return False
    try:
        r = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, timeout=120,
                           check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _surface(grid):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return grid.extract_surface()


def _render_3d(case: Case, regions: dict, patches: dict, out_dir: Path,
               max_seeds: int = 150) -> list[Path]:
    import pyvista as pv

    out: list[Path] = []
    air = regions.get("air")
    solids = [(n, g) for n, g in regions.items() if n != "air" and "T" in g.cell_data]
    T_all = [np.asarray(g.cell_data["T"]) - KELVIN for _, g in solids]
    E = case.enclosure.as_tuple()
    outline = pv.Box(bounds=(0, E[0], 0, E[1], 0, E[2])).outline()

    def plotter():
        p = pv.Plotter(off_screen=True, window_size=(1200, 800))
        p.set_background("white")
        p.add_mesh(outline, color="black")
        return p

    # component surfaces coloured by temperature
    if solids:
        clim = (float(min(t.min() for t in T_all)), float(max(t.max() for t in T_all)))
        p = plotter()
        for (name, g), t in zip(solids, T_all, strict=True):
            g = g.copy()
            g.cell_data["T [°C]"] = t
            p.add_mesh(_surface(g), scalars="T [°C]", preference="cell", cmap="turbo",
                       clim=clim, show_scalar_bar=False)
        p.add_scalar_bar("T [°C]", color="black")
        p.view_isometric()
        path = out_dir / "components_3d.png"
        p.screenshot(str(path))
        p.close()
        out.append(path)

    # streamlines from the inlets, coloured by temperature
    if air is not None and "U" in air.point_data:
        inlets = [patches[o.name] for o in case.openings
                  if o.kind == OpeningKind.inlet and o.name in patches]
        if inlets:
            pts = np.concatenate([np.asarray(i.cell_centers().points) for i in inlets])
            if len(pts) > max_seeds:  # evenly thin out the seeds
                pts = pts[np.linspace(0, len(pts) - 1, max_seeds).astype(int)]
            # nudge seeds just inside the domain
            centre = np.array(E) / 2
            pts = pts + 1e-4 * (centre - pts) / np.linalg.norm(centre - pts, axis=1)[:, None]
            seeds = pv.PolyData(pts)
            g = air.copy()
            if "T" in g.point_data:
                g.point_data["T [°C]"] = np.asarray(g.point_data["T"]) - KELVIN
            lines = g.streamlines_from_source(seeds, vectors="U", max_length=10 * max(E),
                                              integration_direction="forward")
            if lines.n_points:
                p = plotter()
                scal = "T [°C]" if "T [°C]" in lines.point_data else None
                clim = None
                if scal:  # a few lines graze hot walls: don't let them flatten the scale
                    tl = np.asarray(lines.point_data[scal])
                    clim = (float(tl.min()), max(float(np.percentile(tl, 99)), float(tl.min()) + 1))
                p.add_mesh(lines.tube(radius=max(E) * 0.002) if lines.n_lines else lines,
                           scalars=scal, cmap="turbo", clim=clim, show_scalar_bar=bool(scal))
                for name, s in solids:
                    p.add_mesh(_surface(s), color="lightgrey", opacity=0.6)
                p.view_isometric()
                path = out_dir / "streamlines_3d.png"
                p.screenshot(str(path))
                p.close()
                out.append(path)
    return out


def render_images(case: Case, case_dir: Path, out_dir: Path, three_d: bool | None = None,
                  summary: Summary | None = None) -> list[Path]:
    """Off-screen PNGs: temperature slice, velocity slice, streamlines, component temps.

    three_d: True/False forces the 3D views on/off; None renders them when an OpenGL
    context is available."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _, blocks = _read_blocks(case_dir)
    regions = {r: b["internal"] for r, b in blocks.items()}
    f = _focus_point(case)
    images: list[Path | None] = []

    def to_c(v):
        return v - KELVIN

    def speed(v):
        return np.linalg.norm(v, axis=1)

    images.append(_plane_map(case, regions, 2, f[2], "T", "Temperature, horizontal cut",
                             "T [°C]", out_dir / "temperature_slice_z.png", to_scalar=to_c))
    images.append(_plane_map(case, regions, 1, f[1], "T", "Temperature, vertical cut",
                             "T [°C]", out_dir / "temperature_slice_y.png", to_scalar=to_c))
    images.append(_plane_map(case, regions, 2, f[2], "U", "Air velocity, horizontal cut",
                             "|U| [m/s]", out_dir / "velocity_slice_z.png", air_only=True,
                             to_scalar=speed, quiver=True, cmap="viridis"))
    images.append(_plane_map(case, regions, 1, f[1], "U", "Air velocity, vertical cut",
                             "|U| [m/s]", out_dir / "velocity_slice_y.png", air_only=True,
                             to_scalar=speed, quiver=True, cmap="viridis"))
    s = summary if summary is not None else summarize(case, case_dir)
    images.append(_bar_chart(s, out_dir / "component_temperatures.png"))

    if three_d is None:
        three_d = rendering_available()
    if three_d:
        images.extend(_render_3d(case, regions, blocks["air"]["patches"], out_dir))
    return [p for p in images if p is not None]


# --------------------------------------------------------------------------- report

_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:24px auto;
max-width:960px;padding:0 16px;color:#1d1d1f;background:#fff}
h1{font-size:1.6em;margin-bottom:0}.sub{color:#666;margin-top:4px}
table{border-collapse:collapse;margin:12px 0;width:100%}
th,td{border-bottom:1px solid #ddd;padding:6px 10px;text-align:left}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.kpi{border:1px solid #ddd;border-radius:8px;padding:10px}
.kpi b{display:block;font-size:1.4em}.kpi span{color:#666;font-size:.85em}
.warn{background:#fff4e5;border-left:4px solid #f0a020;padding:8px 12px;margin:6px 0}
img{max-width:100%;border:1px solid #eee;margin:8px 0}
"""


def _fmt(v: float, nd: int = 1) -> str:
    return "n/a" if v is None or not np.isfinite(v) else f"{v:.{nd}f}"


def _energy_html(s: Summary) -> str:
    b = s.energy
    if b is None:
        return ""
    e = html.escape
    rows = []
    for p in b.parts:
        others = ", ".join(f"{e(n)} {w:+.2f}" for n, w in p.to_parts_w.items() if abs(w) >= 0.005)
        rows.append(f"<tr><td>{e(p.name)}</td><td class=n>{_fmt(p.power_w, 2)}</td>"
                    f"<td class=n>{_fmt(p.to_air_w, 2)}</td><td>{others or '-'}</td>"
                    f"<td class=n>{_fmt(p.to_walls_w, 2)}</td>"
                    f"<td class=n>{_fmt(p.residual_w, 2)}</td></tr>")
    out = " · ".join(f"{e(n)} {_fmt(w, 2)} W" for n, w in b.air_to_openings_w.items())
    return f"""<h2>Energy balance</h2>
<table><tr><th>Component</th><th class=n>Power [W]</th><th class=n>To air [W]</th>
<th>To other parts [W]</th><th class=n>To walls [W]</th><th class=n>Unbalanced [W]</th></tr>
{''.join(rows)}</table>
<p class=sub>Carried out by the air: {out or 'n/a'} · through the enclosure walls
{_fmt(b.air_to_walls_w + sum(p.to_walls_w for p in b.parts), 2)} W ·
not accounted for {_fmt(b.residual_w, 2)} W ({_fmt(100 * b.error, 1)} % of
{_fmt(b.total_power_w, 2)} W).</p>"""


def _openings_html(s: Summary) -> str:
    if not s.openings:
        return ""
    e = html.escape
    rows = "".join(
        f"<tr><td>{e(o.name)}</td><td>{e(o.kind)}</td>"
        f"<td class=n>{_fmt(o.mass_flow_kg_s * 1000, 3)}</td><td class=n>{_fmt(o.velocity_mean, 2)}</td>"
        f"<td class=n>{_fmt(o.t_mean_c)}</td><td class=n>{_fmt(o.p_static_pa, 2)}</td>"
        f"<td class=n>{_fmt(o.p_total_pa, 2)}</td></tr>" for o in s.openings)
    return f"""<h2>Openings</h2>
<table><tr><th>Opening</th><th>Kind</th><th class=n>Mass flow in [g/s]</th>
<th class=n>Mean speed [m/s]</th><th class=n>T [°C]</th><th class=n>Static p [Pa]</th>
<th class=n>Total p [Pa]</th></tr>{rows}</table>
<p class=sub>Pressures are gauge, relative to the ambient pressure. Pressure drop inlet to
outlet: {_fmt(s.pressure_drop_pa, 2)} Pa static ({e(s.pressure_field)}),
{_fmt(s.pressure_drop_total_pa, 2)} Pa total.</p>"""


def write_report(case: Case, case_dir: Path, out_dir: Path, images: list[Path] | None = None,
                 summary: Summary | None = None) -> Path:
    """HTML report (numbers + images). Returns its path. Images are embedded, so the
    file can be moved or shared on its own. Also writes summary.json next to it."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    s = summary if summary is not None else summarize(case, case_dir)
    img_warn = None
    if images is None:
        try:
            images = render_images(case, case_dir, out_dir, summary=s)
        except Exception as e:  # noqa: BLE001 (numbers are still worth a report)
            images, img_warn = [], f"images could not be rendered: {e}"

    (out_dir / "summary.json").write_text(json.dumps(s.to_dict(), indent=2, default=float))

    e = html.escape
    kpis = [
        ("Hottest component", f"{_fmt(s.hottest().t_max_c)} °C" if s.hottest() else "n/a",
         s.hottest().name if s.hottest() else ""),
        ("Max air temperature", f"{_fmt(s.air_t_max_c)} °C", ""),
        ("Outlet air (mean)", f"{_fmt(s.outlet_t_mean_c)} °C",
         f"inlet {_fmt(s.inlet_t_mean_c)} °C"),
        ("Max air velocity", f"{_fmt(s.air_v_max, 2)} m/s", ""),
        ("Pressure drop", f"{_fmt(s.pressure_drop_pa, 2)} Pa",
         f"static ({s.pressure_field}), {_fmt(s.pressure_drop_total_pa, 2)} Pa total"),
        ("Energy balance", f"{_fmt(s.heat_to_air_w)} W",
         f"of {_fmt(s.total_power_w)} W dissipated ({_fmt(100 * s.energy_balance_error, 0)} %)"),
    ]
    rows = "\n".join(
        f"<tr><td>{e(c.name)}</td><td class=n>{_fmt(c.power_w)}</td>"
        f"<td class=n>{_fmt(c.t_max_c)}</td><td class=n>{_fmt(c.t_mean_c)}</td></tr>"
        for c in sorted(s.components, key=lambda c: -c.t_max_c if np.isfinite(c.t_max_c) else 0))
    warns = s.warnings + ([img_warn] if img_warn else [])
    imgs = []
    for p in images:
        data = base64.b64encode(Path(p).read_bytes()).decode()
        title = Path(p).stem.replace("_", " ").capitalize()
        imgs.append(f"<figure><img alt='{e(title)}' src='data:image/png;base64,{data}'>"
                    f"<figcaption>{e(title)}</figcaption></figure>")
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(case.name)} results</title><style>{_CSS}</style></head><body>
<h1>{e(case.name)}: simulation results</h1>
<p class=sub>Air Construct · time {s.time:g} · ambient {case.ambient_temperature:g} °C ·
generated {datetime.now().astimezone():%Y-%m-%d %H:%M}</p>
{''.join(f'<div class=warn>{e(w)}</div>' for w in warns)}
<div class=kpis>{''.join(f'<div class=kpi><span>{e(a)}</span><b>{e(b)}</b><span>{e(c)}</span></div>'
                         for a, b, c in kpis)}</div>
<h2>Components</h2>
<table><tr><th>Component</th><th class=n>Power [W]</th><th class=n>T max [°C]</th>
<th class=n>T mean [°C]</th></tr>
{rows}</table>
<p class=sub>Mass flow: {_fmt(s.mass_flow_kg_s * 1000, 3)} g/s
(outlet vs inlet: {_fmt(100 * s.mass_imbalance, 1)} %).</p>
{_energy_html(s)}
{_openings_html(s)}
<h2>Fields</h2>
{''.join(imgs) or '<p>No images.</p>'}
</body></html>
"""
    path = out_dir / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path
