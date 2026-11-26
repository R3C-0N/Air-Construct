"""Probes: case model, function objects, reading the histories (no OpenFOAM)."""

import pytest
from test_case import EXAMPLE

from airconstruct.case import Case
from airconstruct.foam.writer import write_case_setup
from airconstruct.probes import read_probes


@pytest.fixture
def case():
    d = Case.load(EXAMPLE).model_dump(mode="json")
    d["probes"] = [
        {"name": "above_cpu", "point": {"x": 0.075, "y": 0.055, "z": 0.04}},
        {"name": "in_cpu", "point": {"x": 0.075, "y": 0.055, "z": 0.0085}},
        {"name": "outlet_air", "point": {"x": 0.195, "y": 0.06, "z": 0.03}},
    ]
    return Case.model_validate(d)


def test_probe_regions(case):
    assert [case.probe_region(p) for p in case.probes] == ["air", "cpu", "air"]


def test_probe_validation(case):
    d = case.model_dump(mode="json")
    d["probes"][0]["point"]["z"] = 1.0
    with pytest.raises(ValueError):
        Case.model_validate(d)
    d = case.model_dump(mode="json")
    d["probes"][1]["name"] = "above_cpu"
    with pytest.raises(ValueError):
        Case.model_validate(d)


def test_probe_function_objects(case, tmp_path):
    case.solver.probe_interval = 5
    write_case_setup(case, tmp_path)
    cd = (tmp_path / "system/controlDict").read_text()
    assert "probes_air" in cd and "probes_cpu" in cd
    assert "probeLocations  ((0.075 0.055 0.04) (0.195 0.06 0.03));" in cd
    assert "fields          (T);" in cd  # solid: temperature only
    assert "writeInterval   5;" in cd
    assert "stopAtWriteNowSignal 10;" in cd  # long keys keep a space before the value


def _write(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "".join(rows))


def test_read_probes_across_restarts(case, tmp_path):
    hdr_air = ("# Probe 0 (0.075 0.055 0.04)\n# Probe 1 (0.195 0.06 0.03)\n"
               "#           Probe             0             1\n#            Time\n")
    base = tmp_path / "postProcessing" / "air" / "probes_air"
    # flow phase 0..20, then a restart at 20 (energy phase) writing a new folder
    _write(base / "0" / "T", hdr_air, ["10 300.15 299.15\n", "20 301.15 299.65\n"])
    _write(base / "0" / "U", hdr_air, ["10 (0.1 0 0) (1 0 0)\n", "20 (0.3 0.4 0) (1 0 0)\n"])
    _write(base / "20" / "T", hdr_air, ["30 302.15 300.15\n", "40 3"])  # last line cut
    _write(base / "20" / "U", hdr_air, ["30 (0 0 0.5) (1 0 0)\n"])
    hdr_cpu = "# Probe 0 (0.075 0.055 0.0085)\n#            Time\n"
    _write(tmp_path / "postProcessing" / "cpu" / "probes_cpu" / "0" / "T", hdr_cpu,
           ["10 340.15\n", "20 350.15\n"])
    p = read_probes(case, tmp_path)
    a = p["above_cpu"]
    assert a.iterations == [10, 20, 30]
    assert a.t_c == pytest.approx([27.0, 28.0, 29.0])
    assert a.speed == pytest.approx([0.1, 0.5, 0.5])
    assert a.last() == pytest.approx((29.0, 0.5))
    assert p["outlet_air"].t_c[-1] == pytest.approx(27.0)
    c = p["in_cpu"]
    assert c.region == "cpu" and c.t_c == pytest.approx([67.0, 77.0]) and c.u == []


def test_missing_probe_does_not_shift_others(case, tmp_path):
    # OpenFOAM dropped the first air probe: only the second one is in the file
    hdr = "# Probe 0 (0.195 0.06 0.03)\n#            Time\n"
    _write(tmp_path / "postProcessing" / "probes_air" / "0" / "T", hdr, ["10 299.15\n"])
    p = read_probes(case, tmp_path)
    assert p["above_cpu"].t_c == []
    assert p["outlet_air"].t_c == pytest.approx([26.0])
    assert p["in_cpu"].t_c == []
