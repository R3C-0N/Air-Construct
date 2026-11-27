from pathlib import Path

import pytest

from airconstruct.case import Case
from airconstruct.foam.dictionary import Raw, fmt_entries

EXAMPLE = Path(__file__).parent.parent / "examples" / "electronics_box.yaml"


def test_example_loads():
    c = Case.load(EXAMPLE)
    assert c.solid_regions == ["board", "cpu", "heatsink", "regulator", "ram"]
    inlet = next(o for o in c.openings if o.kind == "inlet")
    assert inlet.normal_velocity() == pytest.approx(1.0)


def test_roundtrip(tmp_path):
    c = Case.load(EXAMPLE)
    c.save(tmp_path / "c.yaml")
    assert Case.load(tmp_path / "c.yaml") == c


def test_component_outside_rejected():
    data = Case.load(EXAMPLE).model_dump(mode="json")
    data["components"][0]["box"]["origin"]["x"] = 0.5
    with pytest.raises(ValueError):
        Case.model_validate(data)


def test_needs_outlet():
    data = Case.load(EXAMPLE).model_dump(mode="json")
    data["openings"] = [o for o in data["openings"] if o["kind"] == "inlet"]
    with pytest.raises(ValueError):
        Case.model_validate(data)


def test_foam_dict_format():
    txt = fmt_entries({"a": 1, "b": {"c": [1, 2, 3]}, ".*_to_.*": {"type": "x"}, "i": Raw("#include \"f\"")})
    assert "a               1;" in txt
    assert "(1 2 3);" in txt
    assert '".*_to_.*"' in txt
    assert '#include "f"' in txt


def _case_with(**extra):
    data = Case.load(EXAMPLE).model_dump(mode="json")
    data.update(extra)
    return data


def test_custom_material_is_used_by_components():
    data = _case_with(materials=[{"name": "graphite_pad", "kappa": 5.0, "rho": 1800.0,
                                  "cp": 700.0}])
    data["components"][0]["material"] = "graphite_pad"
    case = Case.model_validate(data)
    assert case.solid("graphite_pad").kappa == 5.0
    assert case.solid("copper").kappa == 390.0


def test_unknown_or_clashing_material_is_rejected():
    data = _case_with()
    data["components"][0]["material"] = "unobtainium"
    with pytest.raises(ValueError, match="unknown solid material"):
        Case.model_validate(data)
    data = _case_with(materials=[{"name": "copper", "kappa": 1, "rho": 1, "cp": 1}])
    with pytest.raises(ValueError, match="built-in material"):
        Case.model_validate(data)
    data = _case_with(materials=[{"name": "pad", "kappa": 0, "rho": 1, "cp": 1}])
    with pytest.raises(ValueError):
        Case.model_validate(data)


def test_max_temperature_is_optional_and_kept():
    data = _case_with()
    data["components"][0]["max_temperature"] = 85.0
    case = Case.model_validate(data)
    assert case.components[0].max_temperature == 85.0
    assert case.components[1].max_temperature is None
