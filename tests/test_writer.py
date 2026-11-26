from test_case import EXAMPLE

from airconstruct.case import Case
from airconstruct.foam.writer import write_case_setup, write_initial_fields
from airconstruct.runner import ResidualParser
from airconstruct.thermal_estimate import estimate_temperatures


def test_case_files(tmp_path):
    case = Case.load(EXAMPLE)
    write_case_setup(case, tmp_path)
    write_initial_fields(case, tmp_path)
    rp = (tmp_path / "constant/regionProperties").read_text()
    assert "fluid (air)" in rp and "solid (board cpu heatsink regulator ram)" in rp
    assert (tmp_path / "constant/cpu/fvOptions").exists()  # heat source
    assert not (tmp_path / "constant/board/fvOptions").exists()  # unpowered
    assert "(8 0)" in (tmp_path / "constant/cpu/fvOptions").read_text()
    u = (tmp_path / "0/air/U").read_text()
    assert "uniform (1 0 0)" in u  # 0.0032 m3/s through 0.08 x 0.04 m, along +x
    assert '"air_to_.*"' in (tmp_path / "0/air/T").read_text()
    for f in ["U", "p", "p_rgh", "T", "k", "omega", "nut", "alphat"]:
        assert (tmp_path / "0/air" / f).exists()
    assert "frozenFlow      false;" in (tmp_path / "system/air/fvSolution").read_text()


def test_laminar_has_no_turbulence_fields(tmp_path):
    case = Case.load(EXAMPLE)
    case.solver.turbulence = "laminar"
    write_case_setup(case, tmp_path)
    write_initial_fields(case, tmp_path)
    assert not (tmp_path / "0/air/k").exists()


def test_lumped_estimate_is_sane():
    case = Case.load(EXAMPLE)
    t = estimate_temperatures(case)
    assert all(v > case.ambient_temperature for v in t.values())
    assert t["cpu"] > t["board"]


LOG = """Time = 1

Solving for fluid region air
DILUPBiCGStab:  Solving for Ux, Initial residual = 1e-05, Final residual = 1e-07, No Iterations 1
GAMG:  Solving for p_rgh, Initial residual = 2e-05, Final residual = 1e-07, No Iterations 1
Min/max T:298.15 310

Solving for solid region cpu
DICPCG:  Solving for h, Initial residual = 0.3, Final residual = 1e-11, No Iterations 8
Min/max T:320 330.5
ExecutionTime = 0.1 s  ClockTime = 0 s
"""


def test_residual_parser():
    seen = []
    p = ResidualParser(lambda i, r: seen.append((i, r)), tol=1e-3, window=2)
    for k in range(4):
        for line in LOG.replace("Time = 1", f"Time = {k + 1}").splitlines():
            p.feed(line)
    p.close()
    assert seen[-1][0] == 4 and seen[-1][1]["h"] == 0.3
    assert p.flow_converged()  # solid h residual is ignored on purpose
    assert p.temperatures_stable()
    assert round(p.temperatures_c()["cpu"][1], 2) == 57.35
