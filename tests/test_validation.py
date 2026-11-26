"""Validation checks (unit tests with a synthetic summary; the real run is
tests/test_validation_run.py, marked openfoam)."""

from pathlib import Path

import pytest

from airconstruct.case import Case
from airconstruct.materials import GASES
from airconstruct.results import ComponentResult, Summary
from airconstruct.validation import (
    flat_plate_h,
    format_report,
    inlet_mass_flow,
    run_checks,
    wetted_area,
)

CASE = Path(__file__).parent.parent / "validation" / "heated_blocks" / "case.yaml"


def synthetic(case: Case, t_a=70.0, t_b=70.0, t_c=76.0, energy=1.0, mass=1.0) -> Summary:
    m = inlet_mass_flow(case) * mass
    power = sum(c.power for c in case.components)
    rise = power * energy / (inlet_mass_flow(case) * GASES["air"].cp)
    comps = [ComponentResult(n, 1.0, t + 0.1, t) for n, t in
             (("block_a", t_a), ("block_b", t_b), ("block_c", t_c))]
    return Summary(comps, air_t_max_c=t_c, air_v_max=1.5, pressure_drop_pa=0.5,
                   outlet_t_mean_c=20.0 + rise, inlet_t_mean_c=20.0,
                   mass_flow_kg_s=inlet_mass_flow(case), mass_imbalance=mass - 1.0,
                   heat_to_air_w=m * GASES["air"].cp * rise, total_power_w=power)


def test_case_loads():
    case = Case.load(CASE)
    assert case.solver.turbulence == "laminar"
    assert [c.name for c in case.components] == ["block_a", "block_b", "block_c"]


def test_reference_values():
    case = Case.load(CASE)
    # 0.3 m/s through 0.10 x 0.02 m at 20 degC: rho = 1.204 kg/m3
    assert inlet_mass_flow(case) == pytest.approx(1.204 * 0.3 * 0.002, rel=2e-3)
    a = case.components[0]
    assert wetted_area(case, a) == pytest.approx(4 * 0.02 * 0.01 + 0.02 * 0.02)  # floor face excluded
    h = flat_plate_h(case, a, 0.3, 20.0)  # Re ~ 400 over 2 cm: h ~ 15 W/m2K
    assert 12 < h < 18


def test_good_run_passes():
    case = Case.load(CASE)
    checks = run_checks(case, synthetic(case))
    assert len(checks) == 9
    bad = [c.name for c in checks if not c.ok]
    assert not bad
    assert "PASS" in format_report(case, synthetic(case), checks)


@pytest.mark.parametrize("kw, failing", [
    ({"energy": 0.95}, "energy balance"),
    ({"mass": 1.01}, "mass balance"),
    ({"t_b": 72.0}, "symmetry"),
    ({"t_c": 65.0}, "wake"),
    ({"t_a": 300.0, "t_b": 300.0, "t_c": 310.0}, "block_a: h"),
])
def test_bad_run_fails(kw, failing):
    case = Case.load(CASE)
    checks = run_checks(case, synthetic(case, **kw))
    assert any(c.name.startswith(failing) and not c.ok for c in checks)


def test_flux_stats_from_face_fluxes(tmp_path):
    # results.summarize takes mass flow and outlet temperature from phi, not from the
    # boundary velocity (which overestimated both by ~20 % on this case)
    from airconstruct.results import _flux_stats

    d = tmp_path / "100" / "air"
    d.mkdir(parents=True)
    header = "FoamFile\n{\n    object phi;\n}\nboundaryField\n{\n"
    (d / "phi").write_text(header + (
        "    inlet\n    {\n        type calculated;\n"
        "        value nonuniform List<scalar> 2(-0.001 -0.003);\n    }\n"
        "    outlet\n    {\n        type calculated;\n"
        "        value nonuniform List<scalar> \n2\n(\n0.001\n0.003\n)\n;\n    }\n}\n"))
    (d / "T").write_text(header + (
        "    inlet\n    {\n        type fixedValue;\n        value uniform 293.15;\n    }\n"
        "    outlet\n    {\n        type inletOutlet;\n        inletValue uniform 293.15;\n"
        "        value nonuniform List<scalar> 2(303.15 299.15);\n    }\n}\n"))
    m, t = _flux_stats(tmp_path, 100.0, ["outlet"])
    assert m == pytest.approx(0.004)
    assert t == pytest.approx((0.001 * 303.15 + 0.003 * 299.15) / 0.004)
    m, t = _flux_stats(tmp_path, 100.0, ["inlet"])
    assert (m, t) == (pytest.approx(0.004), pytest.approx(293.15))
    assert _flux_stats(tmp_path, 100.0, ["nope"]) is None
