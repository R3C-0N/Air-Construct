"""Live monitoring: function object definitions and parsing of the solver output."""

import json
import math
import re

import pytest
from test_case import EXAMPLE

from airconstruct.case import Case
from airconstruct.foam.monitors import monitor_objects, write_monitors
from airconstruct.foam.writer import write_case_setup
from airconstruct.monitor import Progress, RunMonitor, read_history, residual_series

# one iteration of chtMultiRegionSimpleFoam (openfoam.com) with the monitors on
ITERATION = """Time = {i}


Solving for fluid region air
DILUPBiCGStab:  Solving for Ux, Initial residual = 0.0013, Final residual = 1.2e-05, No Iterations 1
DILUPBiCGStab:  Solving for Uy, Initial residual = 0.0048, Final residual = 4.1e-05, No Iterations 1
DILUPBiCGStab:  Solving for h, Initial residual = 0.00044, Final residual = 7.1e-11, No Iterations 12
Min/max T:298.15 361.42
GAMG:  Solving for p_rgh, Initial residual = 0.0055, Final residual = 9.6e-07, No Iterations 3
time step continuity errors : sum local = 2.1e-06, global = 5.9e-08, cumulative = -0.0003
GAMG:  Solving for p_rgh, Initial residual = 0.0002, Final residual = 9.6e-07, No Iterations 3
Min/max rho:0.97 1.20

Solving for solid region cpu
DICPCG:  Solving for h, Initial residual = 2.7e-06, Final residual = 3.7e-11, No Iterations 19
Min/max T:350.15 360.15

Solving for solid region ram
DICPCG:  Solving for h, Initial residual = 1.1e-05, Final residual = 3.7e-11, No Iterations 19
Min/max T:320.15 321.15
ExecutionTime = 3.25 s  ClockTime = 4 s

volFieldValue T_air write:
    volAverage() of T = 300.15

volFieldValue T_cpu write:
    volAverage() of T = 355.15

wallHeatFlux heat_air write:
    min/max/integ(walls) = 0, 0, -0.5
    min/max/integ(air_to_cpu) = -10, 2000, 8.25
    min/max/integ(air_to_ram) = -10, 200, 2.25

surfaceFieldValue flow_inlet write:
    total faces   = 320
    total area    = 0.0032
    sum(inlet) of phi = -0.004

surfaceFieldValue T_inlet write:
    weightedAverage(inlet) of T = 298.15

surfaceFieldValue p_inlet write:
    areaAverage(inlet) of p_rgh = 101326.5

surfaceFieldValue flow_outlet write:
    sum(outlet) of phi = 0.004

surfaceFieldValue T_outlet write:
    weightedAverage(outlet) of T = 300.65

surfaceFieldValue p_outlet write:
    areaAverage(outlet) of p_rgh = 101325.25

"""


@pytest.fixture
def case():
    return Case.load(EXAMPLE)


def test_monitor_objects(case):
    objs = monitor_objects(case)
    regions = ["air"] + [c.name for c in case.components]
    assert {f"T_{r}" for r in regions} <= set(objs)
    assert objs["T_cpu"]["region"] == "cpu" and objs["T_cpu"]["operation"] == "volAverage"
    assert objs["heat_air"]["type"] == "wallHeatFlux" and objs["heat_air"]["region"] == "air"
    for o in case.openings:
        assert objs[f"flow_{o.name}"]["name"] == o.name
        assert objs[f"T_{o.name}"]["weightField"] == "phi"
    assert all(o["log"] is True for o in objs.values())


def test_control_dict_includes_the_monitors(case, tmp_path):
    write_case_setup(case, tmp_path)
    cd = (tmp_path / "system" / "controlDict").read_text()
    assert re.search(r"functions\s*\{\s*#includeIfPresent \"monitors\"\s*\}", cd)
    # no key glued to its value (16+ character keys)
    assert "writeCompression off;" in cd and "runTimeModifiable true;" in cd
    path = write_monitors(case, tmp_path)
    text = path.read_text()
    assert "T_cpu" in text and "heat_air" in text and "fieldFunctionObjects" in text
    assert "T_cpu" not in write_monitors(case, tmp_path, enabled=False).read_text()


def _feed(mon, iterations):
    for i in iterations:
        for line in ITERATION.format(i=i).splitlines():
            mon.feed(line)
    mon.flush()


def test_progress_snapshot(case, tmp_path):
    seen = []
    mon = RunMonitor(case, seen.append, tmp_path / "monitor.jsonl")
    mon.phase = "flow"
    _feed(mon, [1, 2])
    assert [p.iteration for p in seen] == [1, 2]
    p = seen[-1]
    assert p.phase == "flow"
    assert p.residuals["air"] == {"Ux": 0.0013, "Uy": 0.0048, "h": 0.00044, "p_rgh": 0.0055}
    assert p.residuals["cpu"] == {"h": 2.7e-06}
    t = p.temperatures["cpu"]
    assert (t["min_c"], t["mean_c"], t["max_c"]) == pytest.approx((77.0, 82.0, 87.0))
    assert math.isnan(p.temperatures["ram"]["mean_c"])  # no T_ram output: min/max only
    assert p.temperatures["ram"]["max_c"] == pytest.approx(48.0)
    assert p.heat_to_air_w == {"walls": -0.5, "air_to_cpu": 8.25, "air_to_ram": 2.25}
    assert p.part_heat_w() == {"cpu": 8.25, "ram": 2.25}
    assert p.openings["inlet"]["mass_flow_kg_s"] == pytest.approx(0.004)
    assert p.openings["outlet"]["mass_flow_kg_s"] == pytest.approx(-0.004)
    assert p.openings["outlet"]["t_c"] == pytest.approx(27.5)
    assert p.pressure_drop_pa == pytest.approx(1.25)
    assert p.heat_out_w == pytest.approx(0.004 * 1005.0 * 2.5)


def test_history_file(case, tmp_path):
    mon = RunMonitor(case, None, tmp_path / "monitor.jsonl")
    _feed(mon, [1, 2, 3])
    with open(tmp_path / "monitor.jsonl", "a") as fh:
        fh.write('{"iteration": 4, "resid')  # a line cut by a crash
    hist = read_history(tmp_path)
    assert [p.iteration for p in hist] == [1, 2, 3]
    assert hist[0] == Progress.from_dict(json.loads(
        (tmp_path / "monitor.jsonl").read_text().splitlines()[0]))
    series = residual_series(hist)
    assert series["h@cpu"] == ([1, 2, 3], [2.7e-06] * 3)
    assert series["p_rgh@air"][1] == [0.0055] * 3
    assert read_history(tmp_path / "missing") == []


def test_start_up_output_is_not_an_iteration(case):
    seen = []
    mon = RunMonitor(case, seen.append)
    for line in ["volFieldValue T_air write:", "    volAverage() of T = 300", ""]:
        mon.feed(line)
    _feed(mon, [7])
    assert [p.iteration for p in seen] == [7]
