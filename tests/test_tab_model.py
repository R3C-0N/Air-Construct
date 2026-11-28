"""Modèle tab: geometry operations (pure functions) and the tab in the window."""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from ui_support import SHIPPED_EXAMPLE, make_window

from airconstruct.case import Case
from airconstruct.ui.tabs.model import geometry as geo
from airconstruct.ui.tabs.model.checks import model_alerts
from airconstruct.ui.tabs.model.library import PRESETS, templates

EXAMPLE_DATA = {
    "name": "t", "enclosure": {"x": 0.2, "y": 0.12, "z": 0.06},
    "components": [
        {"name": "board", "material": "fr4",
         "box": {"origin": {"x": 0.01, "y": 0.01, "z": 0.005},
                 "size": {"x": 0.18, "y": 0.10, "z": 0.002}}},
        {"name": "cpu", "material": "silicon", "power": 8.0,
         "box": {"origin": {"x": 0.06, "y": 0.04, "z": 0.007},
                 "size": {"x": 0.03, "y": 0.03, "z": 0.003}}},
    ],
    "openings": [
        {"name": "inlet", "kind": "inlet", "face": "xmin", "center": [0.06, 0.03],
         "size": [0.08, 0.04], "flow_rate": 0.0032},
        {"name": "outlet", "kind": "outlet", "face": "xmax", "center": [0.06, 0.03],
         "size": [0.08, 0.04]},
    ],
    "refinements": [{"target": "cpu"}],
}


def data():
    return copy.deepcopy(EXAMPLE_DATA)


def lo(d, n):
    return geo.lo_hi(geo.comp(d, n))[0]


def size(d, n):
    a, b = geo.lo_hi(geo.comp(d, n))
    return [b[i] - a[i] for i in range(3)]


# ---------------------------------------------------------------------- geometry
def test_unique_names():
    d = data()
    assert geo.unique_name(d, "cpu") == "cpu_2"
    assert geo.unique_name(d, "inlet") == "inlet_2"  # openings count too
    assert geo.unique_name(d, "air") == "air_2"  # reserved
    d["components"].append({**copy.deepcopy(d["components"][1]), "name": "cpu_2"})
    assert geo.unique_name(d, "cpu_2") == "cpu_3"


def test_support_and_drop():
    d = data()
    assert geo.support_of(d, "board") is None  # floats 5 mm above the floor
    assert geo.support_of(d, "cpu") == "board"
    geo.rest_on(d, ["board"], "floor")
    assert lo(d, "board")[2] == 0.0
    assert geo.support_of(d, "board") == "floor"
    # the cpu was not grouped with the board: it stays and now floats; D drops it
    assert geo.support_of(d, "cpu") is None
    geo.rest_on(d, ["cpu"])
    assert lo(d, "cpu")[2] == pytest.approx(0.002)
    assert geo.support_of(d, "cpu") == "board"


def test_rest_on_a_part_not_under_it_centres_it():
    d = data()
    geo.add_part(d, PRESETS[-1].part(), "block")  # bloc 20 mm on the floor, centred
    geo.move(d, ["bloc"], [0.08, 0, 0])
    geo.rest_on(d, ["bloc"], "cpu")
    assert lo(d, "bloc")[2] == pytest.approx(0.010)
    assert lo(d, "bloc")[0] == pytest.approx(0.065)  # centred on the cpu (60..90 mm)


def test_add_part_on_selected_part_and_fit_in_box():
    d = data()
    n = geo.add_part(d, PRESETS[1].part(), "chip", on="board")
    assert n == "puce"
    assert lo(d, n)[2] == pytest.approx(0.007)
    assert geo.read_meta(d)["kinds"][n] == "chip"
    big = PRESETS[0].part()  # 160 x 100 mm board in a 50 mm cube
    d["enclosure"] = {"x": 0.05, "y": 0.05, "z": 0.05}
    n = geo.add_part(d, big, "board")
    assert size(d, n)[0] == pytest.approx(0.05)


def test_move_group_together_and_locked_stays():
    d = data()
    geo.group(d, ["board", "cpu"])
    geo.set_position(d, ["cpu"], 0, 0.0)  # moving the cpu drags its group
    assert lo(d, "board")[0] == pytest.approx(0.0)
    assert lo(d, "cpu")[0] == pytest.approx(0.05)
    geo.set_flag(d, "locked", ["board"], True)
    geo.move(d, ["cpu"], [0.01, 0, 0])
    assert lo(d, "board")[0] == pytest.approx(0.0)
    assert lo(d, "cpu")[0] == pytest.approx(0.06)
    geo.ungroup(d, ["cpu"])
    assert geo.read_meta(d)["groups"] == {}


def test_apply_bounds_snaps():
    d = data()
    b = geo.lo_hi(geo.comp(d, "cpu"))
    # dragged by 12.3 mm in x: moved by 10 mm (5 mm steps); y, z not dragged: kept
    geo.apply_bounds(d, "cpu", (b[0][0] + 0.0123, b[1][0] + 0.0123, b[0][1], b[1][1],
                                b[0][2], b[1][2]), "move", 0.005)
    assert lo(d, "cpu") == pytest.approx([0.070, 0.04, 0.007])
    assert size(d, "cpu") == pytest.approx([0.03, 0.03, 0.003])
    # resize the +x face only: snapped, the other faces stay (z thinner than the grid)
    a, z = geo.lo_hi(geo.comp(d, "cpu"))
    geo.apply_bounds(d, "cpu", (a[0], z[0] + 0.0121, a[1], z[1], a[2], z[2]), "resize", 0.005)
    assert lo(d, "cpu")[0] == pytest.approx(0.070)
    assert size(d, "cpu")[0] == pytest.approx(0.040)  # +x face 112.1 -> 110 mm
    assert size(d, "cpu")[2] == pytest.approx(0.003)
    # a face dragged past the other one does not flip the part
    a, z = geo.lo_hi(geo.comp(d, "cpu"))
    geo.apply_bounds(d, "cpu", (z[0] + 0.01, z[0], a[1], z[1], a[2], z[2]), "resize", 0.005)
    assert size(d, "cpu")[0] == pytest.approx(0.005)
    # free move (no grid)
    geo.apply_bounds(d, "cpu", (0.0711, 0.0761, a[1], z[1], a[2], z[2]), "move", None)
    assert lo(d, "cpu")[0] == pytest.approx(0.0711)


def test_duplicate_array_delete_rename():
    d = data()
    new = geo.duplicate(d, ["cpu"])
    assert new == ["cpu_2"]
    assert lo(d, "cpu_2")[0] > lo(d, "cpu")[0] + 0.03 - 1e-9  # placed beside it
    assert {r["target"] for r in d["refinements"]} == {"cpu", "cpu_2"}
    assert not geo.overlaps(d)
    new = geo.linear_array(d, ["cpu"], 3, 1, 0.035)
    assert len(new) == 2
    assert lo(d, new[1])[1] == pytest.approx(0.04 + 0.07)
    geo.group(d, ["cpu", "cpu_2"])
    geo.rename(d, "cpu", "processeur")
    assert geo.read_meta(d)["groups"]["groupe"] == ["processeur", "cpu_2"]
    assert d["refinements"][0]["target"] == "processeur"
    assert geo.rename_error(d, "processeur", "cpu_2")
    assert geo.rename_error(d, "processeur", "2x")
    assert geo.rename_error(d, "processeur", "ok_name") is None
    geo.set_flag(d, "hidden", ["cpu_2"], True)
    geo.delete(d, ["cpu_2", "processeur"])
    assert "processeur" not in geo.names(d)
    assert geo.read_meta(d)["groups"] == {} and geo.read_meta(d)["hidden"] == []
    assert all(r["target"] not in ("processeur", "cpu_2") for r in d["refinements"])


def test_duplicate_whole_group_makes_a_new_group():
    d = data()
    geo.group(d, ["board", "cpu"], "carte_mere")
    d["enclosure"]["z"] = 0.06
    new = geo.duplicate(d, ["cpu"], [0, 0, 0.02])
    assert sorted(new) == ["board_2", "cpu_2"]
    groups = geo.read_meta(d)["groups"]
    assert sorted(groups) == ["carte_mere", "carte_mere_2"]


def test_align_and_distribute():
    d = data()
    for x in (0.02, 0.05, 0.15):
        n = geo.add_part(d, PRESETS[-1].part(), "block")
        geo.set_position(d, [n], 0, x)
    blocks = ["bloc", "bloc_2", "bloc_3"]
    geo.align(d, blocks, "xdist")
    xs = [lo(d, n)[0] for n in blocks]
    assert xs == pytest.approx([0.02, 0.085, 0.15])
    geo.align(d, blocks, "xmax")
    assert {round(lo(d, n)[0], 6) for n in blocks} == {0.15}


def test_overlap_and_outside_checks():
    d = data()
    assert geo.overlaps(d) == [] and geo.outside(d) == []
    geo.move(d, ["cpu"], [0, 0, -0.001])  # sinks 1 mm into the board
    [(a, b, depth)] = geo.overlaps(d)
    assert (a, b) == ("board", "cpu") and depth[2] == pytest.approx(0.001)
    geo.move(d, ["cpu"], [0.12, 0, 0.001])
    assert geo.outside(d) == [("cpu", 0, pytest.approx(0.01))]
    alerts = model_alerts(d)
    assert any("dépasse de l’enceinte de 10 mm en x" in a.message and a.level == "error"
               for a in alerts)
    # the validation error already names that part: not reported twice
    err = ValueError("component 'cpu' is outside the enclosure")
    assert not any("dépasse" in a.message for a in model_alerts(d, err))


def test_overlap_alert_text_and_tmax_check():
    d = data()
    geo.move(d, ["cpu"], [0, 0, -0.002])
    d["ambient_temperature"] = 40
    d["components"][1]["max_temperature"] = 35
    msgs = [a.message for a in model_alerts(d)]
    assert "board chevauche cpu (recouvrement de 2 mm en z)" in msgs
    assert any("pas au-dessus de l’ambiante" in m for m in msgs)


def test_templates_are_valid_cases():
    example = Case.load(SHIPPED_EXAMPLE).model_dump(mode="json")
    ts = {t.key: t for t in templates(example)}
    for key in ("board", "rack", "psu"):
        data = ts[key].build("essai")
        case = Case.model_validate(data)
        assert case.name == "essai"
        assert not geo.overlaps(data) and not geo.outside(data), key
    empty = ts["empty"].build("vide")
    assert empty["components"] == [] and empty["openings"] == []
    assert ts["cad"].enabled  # imports a CAD file (test_tab_model_cad.py)


# ---------------------------------------------------------------------- the tab


@pytest.fixture
def win(qapp, tmp_path, settings):
    w = make_window(tmp_path, settings)
    yield w
    w.close()


def test_library_adds_on_selection_and_selects_it(win):
    tab = win.tabs["model"]
    win.selection.set(["component:board"])
    name = tab.add_preset("chip")
    assert name == "puce" and win.selection.keys == ["component:puce"]
    c = next(c for c in win.project.case.components if c.name == "puce")
    assert c.power == 5.0 and c.max_temperature == 85.0
    assert c.box.origin.z == pytest.approx(0.007)
    assert tab.inspector.title.text() == "puce · Puce"
    win.undo_action.trigger()
    assert "puce" not in win.project.case.solid_regions


def test_inspector_multi_selection(win):
    tab = win.tabs["model"]
    insp = tab.inspector
    win.selection.set(["component:cpu", "component:ram"])
    assert insp.title.text() == "2 pièces"
    assert insp.power.edit.text() == "" and insp.power.edit.placeholderText() == "mixte"
    assert insp.material.currentText() == "Silicium"  # both silicon
    assert not insp.name_edit.isVisible()
    insp.power.edit.setText("4")
    insp.power._commit()
    case = win.project.case
    assert [c.power for c in case.components if c.name in ("cpu", "ram")] == [4.0, 4.0]
    # position moves the selection as a block
    insp.pos[1].edit.setText("30")
    insp.pos[1]._commit()
    ys = {c.name: c.box.origin.y for c in win.project.case.components}
    assert ys["cpu"] == pytest.approx(0.03) and ys["ram"] == pytest.approx(0.06)
    # max temperature typed in K when the display is in kelvin
    win.units.set_temperature("K")
    insp.tmax.edit.setText("358,15")
    insp.tmax._commit()
    assert [c.max_temperature for c in win.project.case.components
            if c.name in ("cpu", "ram")] == pytest.approx([85.0, 85.0])
    insp.tmax.edit.setText("")
    insp.tmax._commit()
    assert all(c.max_temperature is None for c in win.project.case.components)


def test_rename_material_and_rest_on(win):
    tab = win.tabs["model"]
    insp = tab.inspector
    win.selection.set(["component:cpu"])
    insp.name_edit.setText("ram")  # taken
    insp._rename()
    assert "déjà pris" in insp.name_error.text()
    insp.name_edit.setText("processeur")
    insp._rename()
    assert "processeur" in win.project.case.solid_regions
    assert win.selection.keys == ["component:processeur"]
    tab.new_material({"name": "pad", "kappa": 6.0, "rho": 2000.0, "cp": 800.0})
    assert win.project.case.solid("pad").kappa == 6.0
    assert next(c for c in win.project.case.components if c.name == "processeur").material \
        == "pad"
    assert insp.material_info.text() == "k 6 · ρ 2000 · cp 800"
    tab.edit_material("pad", {"name": "pad2", "kappa": 7.0, "rho": 2000.0, "cp": 800.0})
    assert next(c for c in win.project.case.components if c.name == "processeur").material \
        == "pad2"
    assert insp.rest_on.currentData() == "board"
    tab.rest_on("floor")
    assert next(c for c in win.project.case.components
                if c.name == "processeur").box.origin.z == 0.0


def test_duplicate_group_hide_isolate_delete(win):
    tab = win.tabs["model"]
    win.selection.set(["component:cpu"])
    assert tab.duplicate() == ["cpu_2"]
    assert win.selection.keys == ["component:cpu_2"]
    win.selection.set(["component:cpu", "component:cpu_2"])
    tab.group()
    assert "groupe" in tab.tree.group_items
    win.selection.set(["component:cpu"])
    tab.toggle_hidden()
    names = {i.name for i in tab.scene()}
    assert "cpu" not in names and "cpu_2" in names
    assert "masqué" in tab.tree._items["component:cpu"].text(1)
    tab.toggle_hidden()
    win.selection.set(["component:ram"])
    tab.toggle_isolate()
    assert {i.name for i in tab.scene()} == {"enclosure", "ram"}
    tab.toggle_isolate()
    win.selection.set(["component:cpu_2"])
    tab.delete()
    assert "cpu_2" not in win.project.case.solid_regions
    assert win.selection.keys == []


def test_array_and_handles(win):
    tab = win.tabs["model"]
    win.selection.set(["component:ram"])
    new = tab.array((3, 1, 0.0))  # 0 pitch -> overlapping copies
    assert len(new) == 2
    assert any("chevauche" in a.message for a in win.alerts.for_tab("model"))
    win.undo_action.trigger()
    win.selection.set(["component:ram"])
    tab.set_mode("move")
    tab.set_snap(10.0)
    c = next(c for c in win.project.case.components if c.name == "ram")
    lo, hi = c.box.min, c.box.max
    tab.handle_bounds((lo[0] + 0.013, hi[0] + 0.013, lo[1], hi[1], lo[2], hi[2]))
    c = next(c for c in win.project.case.components if c.name == "ram")
    assert c.box.origin.x == pytest.approx(0.13)
    assert c.box.origin.z == pytest.approx(0.007)  # still on the board
    tab.set_mode("resize")
    tab.handle_bounds((0.13, 0.181, lo[1], hi[1], lo[2], hi[2]), free=True)
    c = next(c for c in win.project.case.components if c.name == "ram")
    assert c.box.size.x == pytest.approx(0.051)
    assert win.project.undo_stack.undoText().startswith("Redimensionner")


def test_out_of_box_alert_selects_part(win):
    tab = win.tabs["model"]
    win.selection.set(["component:ram"])
    tab.set_position(2, 0.058)
    alerts = win.alerts.for_tab("model")
    assert alerts and all(a.level == "error" for a in alerts)
    rows = tab.checks.alert_rows
    assert rows
    win.selection.clear()
    win._on_alert_clicked(alerts[0])
    assert win.selection.keys == ["component:ram"]
    # the part is still drawn where it is, though the case is invalid
    item = next(i for i in tab.scene() if i.name == "ram")
    assert item.mesh.bounds[5] == pytest.approx(0.062)


def test_new_project_dialog(win, qapp):
    from airconstruct.ui.tabs.model.new_project import NewProjectDialog

    example = win.project.data
    dlg = NewProjectDialog(templates(example), ["/tmp/a.yaml"], win)
    assert dlg.buttons["board"].isChecked() and dlg.name_edit.text() == "electronics_box"
    dlg.choose("rack")
    assert dlg.name_edit.text() == "rack_1U"
    dlg.name_edit.setText("2bad")
    assert not dlg.create_button.isEnabled()
    dlg.name_edit.setText("mon_rack")
    dlg.create()
    assert dlg.data["name"] == "mon_rack"
    dlg.close()
    win.ask_new_project = lambda parent, ex, recent: templates(ex)[2].build("mon_rack")
    assert win.new_project()
    assert win.project.case.name == "mon_rack" and win.project.path is None
    assert "carte_mere" in win.tabs["model"].tree.group_items
    win.ask_new_project = lambda parent, ex, recent: None
    assert not win.new_project()
    win.ask_new_project = lambda parent, ex, recent: templates(ex)[0].build("vide")
    assert win.new_project()
    assert win.project.case is None and win.tab_badge("air")  # no inlet yet: Air says so
