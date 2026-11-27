"""Failure recognition on synthetic logs and meshes (no OpenFOAM needed)."""

from pathlib import Path

import pytest
from test_case import EXAMPLE

from airconstruct.case import Case
from airconstruct.diagnose import check_case, check_mesh, diagnose
from airconstruct.monitor import Progress
from airconstruct.runner import RunResult


@pytest.fixture
def case():
    return Case.load(EXAMPLE)


def _result(d, ok=False, converged=False, stopped=False, errors=(), iterations=500):
    return RunResult(Path(d), ok, converged, iterations, 1.0, list(errors), stopped=stopped)


def _log(d, name, text):
    (Path(d) / f"log.{name}").write_text(text)


def _boundary(d, region, patches):
    p = Path(d) / "constant" / region / "polyMesh"
    p.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"    {n}\n    {{\n        type wall;\n        nFaces 1;\n"
                     f"        startFace 0;\n    }}" for n in patches)
    (p / "boundary").write_text(f"FoamFile {{ object boundary; }}\n{len(patches)}\n(\n{body}\n)\n")


SOLVER_FPE = """Time = 12

Solving for fluid region air
DILUPBiCGStab:  Solving for Ux, Initial residual = 0.5, Final residual = 1e-3, No Iterations 1
Min/max T:298 340

Solving for solid region cpu
DICPCG:  Solving for h, Initial residual = 1, Final residual = 1e-11, No Iterations 9
Min/max T:298 9.9e+05
#0  Foam::error::printStack(Foam::Ostream&) at ??:?
#1  Foam::sigFpe::sigHandler(int) at ??:?
Floating point exception (core dumped)
"""


def test_solver_divergence_names_the_part(case, tmp_path):
    _log(tmp_path, "chtMultiRegionSimpleFoam", SOLVER_FPE)
    ds = diagnose(case, tmp_path, _result(tmp_path, errors=["solver failed (exit 136)"]))
    codes = {d.code: d for d in ds}
    assert codes["temperature_runaway"].part == "cpu"
    assert codes["solver_diverged"].part == "cpu"
    assert "cpu" in codes["solver_diverged"].message


def test_fatal_error_in_the_thermo(case, tmp_path):
    _log(tmp_path, "chtMultiRegionSimpleFoam", """Solving for solid region heatsink
DICPCG:  Solving for h, Initial residual = 1, Final residual = 1e-11, No Iterations 9


--> FOAM FATAL ERROR:
Maximum number of iterations exceeded: 100  when starting from T0:400 old T:5000 new T:-200

    From function Foam::species::thermo::T
FOAM exiting
""")
    ds = diagnose(case, tmp_path, _result(tmp_path))
    assert ds[0].code == "temperature_runaway" and ds[0].part == "heatsink"


def test_setup_error_message(case, tmp_path):
    _log(tmp_path, "chtMultiRegionSimpleFoam", """
--> FOAM FATAL IO ERROR:
Unknown patchField type fanPressure for patch outlet

file: 0/air/p_rgh
    From function Foam::fvPatchField::New
""")
    ds = diagnose(case, tmp_path, _result(tmp_path))
    assert ds[0].code == "setup_error"
    assert "Unknown patchField type fanPressure" in ds[0].message


def test_mesh_failure(case, tmp_path):
    _log(tmp_path, "blockMesh", "\n--> FOAM FATAL ERROR:\nZero or negative cell volume\n"
                                "    From function blockMesh\n")
    ds = diagnose(case, tmp_path, _result(tmp_path, errors=["blockMesh failed (exit 1)"]))
    assert ds[0].code == "bad_mesh"


def test_disk_full_and_killed(case, tmp_path):
    _log(tmp_path, "chtMultiRegionSimpleFoam", "write error: No space left on device\n")
    assert diagnose(case, tmp_path, _result(tmp_path))[0].code == "disk_full"
    (tmp_path / "log.chtMultiRegionSimpleFoam").unlink()
    ds = diagnose(case, tmp_path, _result(tmp_path, errors=["x failed (exit -9)\n..."]))
    assert ds[0].code == "out_of_memory"


def test_not_converged_names_the_moving_part(case, tmp_path):
    with open(tmp_path / "monitor.jsonl", "w") as fh:
        import json

        for i in range(100):
            p = Progress(i, temperatures={"air": {"max_c": 40.0 + i},
                                          "cpu": {"max_c": 80.0 + 0.05 * i},
                                          "ram": {"max_c": 50.0 + 0.001 * i}})
            fh.write(json.dumps(p.to_dict()) + "\n")
    ds = diagnose(case, tmp_path, _result(tmp_path, ok=True, iterations=100))
    assert ds[0].code == "not_converged" and ds[0].part == "cpu"
    assert diagnose(case, tmp_path, _result(tmp_path, ok=True, stopped=True))[0].code == "stopped"
    assert diagnose(case, tmp_path, _result(tmp_path, ok=True, converged=True)) == []


def test_overlapping_parts(case):
    assert check_case(case) == []
    case.components[2].box.origin.z = 0.008  # heatsink sinks into the cpu
    d = check_case(case)
    assert d[0].code == "parts_overlap" and d[0].part == "heatsink"


def test_heat_trapped_and_unmeshed(case, tmp_path):
    _boundary(tmp_path, "air", ["walls", "air_to_board", "air_to_heatsink", "air_to_regulator"])
    _boundary(tmp_path, "board", ["board_to_air", "board_to_cpu"])
    _boundary(tmp_path, "cpu", ["cpu_to_board", "cpu_to_heatsink"])
    _boundary(tmp_path, "heatsink", ["heatsink_to_air", "heatsink_to_cpu"])
    _boundary(tmp_path, "regulator", ["regulator_to_air"])
    assert {d.code for d in check_mesh(case, tmp_path)} == {"part_not_meshed"}  # no ram
    _boundary(tmp_path, "ram", ["walls"])  # ram meshed, but touches only the enclosure wall
    ds = check_mesh(case, tmp_path)
    assert [(d.code, d.part) for d in ds] == [("heat_trapped", "ram")]
