"""Detailed energy balance, per-opening flows and total pressure drop, on the synthetic
multi-region case of test_results (exact expected values, no OpenFOAM needed)."""

import json

import numpy as np
import pytest

pytest.importorskip("pyvista")

from foam_fixture import write_case
from test_results import DIVISIONS, DT, EXAMPLE, SOLID_T, T_IN, L, U_field, air_T, p_field

from airconstruct import results
from airconstruct.case import Case
from airconstruct.materials import GASES, SOLIDS

GAS = GASES["air"]
K_AIR = GAS.cp * GAS.mu / GAS.pr  # laminar: no alphat in the synthetic case
G_CPU = 1000.0  # cpu temperature gradient along z [K/m]
G_AIR = DT / L
Z_CPU = 0.006  # bottom of the cpu (on the board)


def T_field(x, region):
    """Air: linear in x. cpu: linear in z, so heat flows in through its top face (air)
    and out through its bottom face (board). Other parts: uniform."""
    if region == "air":
        return air_T(x)
    if region == "cpu":
        return 350.0 + G_CPU * (x[:, 2] - Z_CPU)
    return np.full(len(x), SOLID_T[region])


@pytest.fixture(scope="module")
def case():
    c = Case.load(EXAMPLE)
    c.components[0].box.origin.z = 0.004
    for comp in c.components[1:]:
        comp.box.origin.z = 0.006
    return c


@pytest.fixture(scope="module")
def case_dir(case, tmp_path_factory):
    d = tmp_path_factory.mktemp("energy") / "electronics_box"
    write_case(case, d, DIVISIONS, T=T_field, U=U_field, p=p_field, times=("0", "100"))
    return d


@pytest.fixture(scope="module")
def summary(case, case_dir):
    return results.summarize(case, case_dir)


def _rho0(case):
    return case.ambient_pressure * GAS.mol_weight * 1e-3 / (
        results.R_GAS * (case.ambient_temperature + results.KELVIN))


def test_wall_fluxes_match_the_temperature_gradient(case, case_dir):
    """T linear in z in the cpu: k G A in through its top face (to the air), as much out
    through its bottom face (on the board), nothing through its sides."""
    _, blocks = results._read_blocks(case_dir)
    k = SOLIDS["silicon"].kappa
    q = results._wall_fluxes(case_dir, "cpu", blocks["cpu"], lambda poly: k)
    cpu = next(c for c in case.components if c.name == "cpu")
    a_z = cpu.box.size.x * cpu.box.size.y
    assert q["cpu_to_air"] == pytest.approx(k * G_CPU * a_z, rel=1e-4)
    assert q["cpu_to_board"] == pytest.approx(-k * G_CPU * a_z, rel=1e-4)
    # uniform temperature: no conduction at all
    q_ram = results._wall_fluxes(case_dir, "ram", blocks["ram"], lambda poly: 148.0)
    assert all(abs(v) < 1e-6 for v in q_ram.values())


def test_solid_conductivity_from_the_case_files(case, case_dir, tmp_path):
    assert results._solid_kappa(case, case_dir, "cpu") == SOLIDS["silicon"].kappa
    f = case_dir / "constant" / "cpu" / "thermophysicalProperties"
    f.write_text("mixture { transport { kappa 42.5; } }\n")
    try:
        assert results._solid_kappa(case, case_dir, "cpu") == 42.5
    finally:
        f.unlink()


def test_openings(case, summary):
    by = {o.name: o for o in summary.openings}
    assert set(by) == {"inlet", "outlet"}
    rho0, area = _rho0(case), 0.08 * 0.04
    i, o = by["inlet"], by["outlet"]
    assert i.kind == "inlet" and o.kind == "outlet"
    assert i.area_m2 == pytest.approx(area, rel=1e-6)
    # no phi in the synthetic case: rho U.n A at the ambient density; + = in
    assert i.mass_flow_kg_s == pytest.approx(rho0 * area, rel=1e-5)
    assert o.mass_flow_kg_s == pytest.approx(-rho0 * area, rel=1e-5)
    assert i.velocity_mean == pytest.approx(1.0, rel=1e-5)
    assert i.t_mean_c == pytest.approx(T_IN - results.KELVIN, abs=1e-4)
    assert o.t_mean_c == pytest.approx(T_IN + DT - results.KELVIN, abs=1e-4)
    # gauge p_rgh = 10 Pa - rho g z at the inlet (opening centred at z = 30 mm), 0 at the outlet
    # (pressures are read as float32: ~0.01 Pa resolution around 101325 Pa)
    assert i.p_static_pa == pytest.approx(10.0 - 1.2 * 9.81 * 0.03, abs=0.02)
    assert o.p_static_pa == pytest.approx(-1.2 * 9.81 * 0.03, abs=0.02)
    # same velocity profile in and out (|U|^2 = 1.0625 on average): same dynamic pressure
    dyn = 0.5 * rho0 * (1 + 0.25 * 0.25)
    assert i.p_total_pa == pytest.approx(i.p_static_pa + dyn, abs=1e-6)
    assert summary.pressure_drop_total_pa == pytest.approx(10.0, abs=0.02)
    # the outlet carries m cp dT, minus the (tiny) conduction back into the domain
    q_out = rho0 * area * GAS.cp * DT - K_AIR * G_AIR * area
    assert o.heat_w == pytest.approx(q_out, rel=1e-4)
    assert i.heat_w == pytest.approx(K_AIR * G_AIR * area, rel=1e-3)  # conduction only


def test_energy_balance_per_part(case, summary):
    b = summary.energy
    assert b is not None
    assert [p.name for p in b.parts] == [c.name for c in case.components]
    assert b.total_power_w == 23.0
    by = {p.name: p for p in b.parts}
    # uniform-temperature parts exchange nothing (their faces are at their own T),
    # except on the air side: the air at a part's face has its own T(x)
    assert by["ram"].to_parts_w == pytest.approx(
        {n: 0.0 for n in by["ram"].to_parts_w}, abs=1e-9)
    for p in b.parts:
        assert p.out_w == pytest.approx(p.to_air_w + p.to_walls_w + sum(p.to_parts_w.values()))
        assert p.residual_w == pytest.approx(p.power_w - p.out_w)
    # solid-to-solid flows are antisymmetric
    for p in b.parts:
        for other, w in p.to_parts_w.items():
            assert by[other].to_parts_w[p.name] == pytest.approx(-w)
    assert b.heat_out_w == pytest.approx(sum(b.air_to_openings_w.values()) + b.air_to_walls_w
                                         + sum(p.to_walls_w for p in b.parts))
    assert b.residual_w == pytest.approx(b.total_power_w - b.heat_out_w)
    # the synthetic fields are not a converged solution: the components do not balance
    assert any("has not converged" in w for w in summary.warnings)


def test_sankey(summary):
    b = summary.energy
    s = b.to_sankey(min_w=0.0)
    ids = [n["id"] for n in s["nodes"]]
    assert len(ids) == len(set(ids))
    assert all(link["source"] in ids and link["target"] in ids for link in s["links"])
    assert all(link["value"] > 0 for link in s["links"])
    kinds = {n["id"]: n["kind"] for n in s["nodes"]}
    assert kinds["air"] == "air" and kinds["outlet"] == "opening" and kinds["cpu"] == "part"
    assert next(n for n in s["nodes"] if n["id"] == "cpu")["power_w"] == 15.0
    # every link is a net flow: never both directions between two nodes
    pairs = [(link["source"], link["target"]) for link in s["links"]]
    assert not any((t, s_) in pairs for s_, t in pairs)
    # the heat leaving the enclosure plus the residual link make up the dissipated power
    leaving = sum(link["value"] for link in s["links"] if kinds[link["target"]] in
                  ("opening", "walls"))
    leaving -= sum(link["value"] for link in s["links"] if kinds[link["source"]] in
                   ("opening", "walls"))
    res = sum(link["value"] * (1 if link["target"] == "residual" else -1)
              for link in s["links"] if "residual" in (link["source"], link["target"]))
    assert leaving + res == pytest.approx(b.total_power_w, rel=1e-9)


def test_summary_json_has_the_balance(case, case_dir, summary, tmp_path):
    results.write_report(case, case_dir, tmp_path, images=[], summary=summary)
    data = json.loads((tmp_path / "summary.json").read_text())
    e = data["energy"]
    assert e["total_power_w"] == 23.0
    assert {"residual_w", "heat_out_w", "error", "sankey"} <= set(e)
    assert e["parts"][1]["name"] == "cpu" and "residual_w" in e["parts"][1]
    assert data["openings"][0]["name"] == "inlet"
    assert data["pressure_drop_total_pa"] == pytest.approx(10.0, abs=0.02)
    assert data["components"][1]["t_min_c"] == pytest.approx(
        350 + G_CPU * 0.001 - results.KELVIN, abs=1e-4)  # first cell centre: 1 mm up
    text = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Energy balance" in text and "Openings" in text and "Total p" in text


def test_no_balance_when_the_mesh_cannot_be_read(case, case_dir, monkeypatch):
    monkeypatch.setattr(results, "_foam_list", lambda path: None)  # e.g. binary files
    s = results.summarize(case, case_dir)
    assert s.energy is None
    assert any("energy balance not available" in w for w in s.warnings)
    assert np.isfinite(s.heat_to_air_w)  # the simple balance is still there
