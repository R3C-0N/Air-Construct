from pathlib import Path

import pytest

from airconstruct.case import Case
from airconstruct.foam.dictionary import Raw, fmt_entries

EXAMPLE = Path(__file__).parent.parent / "examples" / "electronics_box.yaml"


def test_example_loads():
    c = Case.load(EXAMPLE)
    assert c.solid_regions == ["board", "cpu", "regulator", "ram"]
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
