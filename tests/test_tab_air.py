"""Air tab: openings on the faces, fans, grilles, walls, checks and quick estimate."""

from __future__ import annotations

import pytest

from airconstruct.case import Case
from airconstruct.ui.tabs.air import geometry as geo
from airconstruct.ui.tabs.air import ops
from airconstruct.ui.tabs.air.checks import air_alerts

FAN_BOX = "examples/fan_box.yaml"


def _data(path=FAN_BOX) -> dict:
    from pathlib import Path

    return Case.load(Path(__file__).parent.parent / path).model_dump(mode="json")


# ---------------------------------------------------------------- geometry (no Qt)
def test_face_size_axes():
    d = _data()  # enclosure 0.20 x 0.12 x 0.06
    assert geo.face_size(d, "xmin") == pytest.approx((0.12, 0.06))
    assert geo.face_size(d, "zmax") == pytest.approx((0.20, 0.12))
    assert geo.face_axis_names("ymin") == ("x", "z")


def test_clamp_and_snap():
    assert geo.clamp_center((0.0, 0.1), (0.02, 0.02), 0.12, 0.06) == pytest.approx((0.01, 0.05))
    c, snapped = geo.snap_center((0.0615, 0.0105), (0.02, 0.02), 0.12, 0.06, tol=0.002)
    assert c == pytest.approx((0.06, 0.01))
    assert snapped == ("center", "edge")
    c, snapped = geo.snap_center((0.05, 0.03), (0.02, 0.02), 0.12, 0.06, tol=0.001)
    assert snapped == (None, "center")


def test_snap_to_other_opening():
    other = geo.Rect(0.0, 0.0, 0.03, 0.06)
    c, snapped = geo.snap_center((0.0415, 0.02), (0.02, 0.02), 0.12, 0.06, tol=0.002,
                                 others=[other])
    assert c[0] == pytest.approx(0.04)  # left edge against the other's right edge
    assert snapped[0] == "opening"


def test_free_spot_avoids_existing_openings():
    d = _data()
    size = (0.03, 0.03)
    c = geo.free_spot(d, "xmin", size)  # the 50 mm fan sits in the middle of xmin
    r = geo.Rect.from_center(c, size)
    assert r.inside(0.12, 0.06)
    fan = geo.opening_rect(d["openings"][0])
    assert r.overlap_area(fan) == 0


def test_nearest_face_and_footprints():
    d = _data()
    assert geo.nearest_face(d, (0.1, 0.06, 0.0599)) == "zmax"
    assert geo.nearest_face(d, (0.0001, 0.06, 0.03)) == "xmin"
    d["components"][0]["box"]["origin"]["z"] = 0.0  # board on the floor
    names = [n for n, _ in geo.component_footprints(d, "zmin")]
    assert names == ["board"]


# ---------------------------------------------------------------- edits (no Qt)
def test_new_openings_make_a_valid_case():
    d = _data()
    d["openings"] = []
    a = ops.new_opening(d, "inlet", "xmin")
    b = ops.new_opening(d, "outlet", "xmax")
    f = ops.new_opening(d, "inlet", "zmax", fan=True)
    assert (a["name"], b["name"], f["name"]) == ("inlet", "outlet", "fan")
    assert a["flow_rate"] > 0 and f["fan"]["curve"]
    case = Case.model_validate(d)
    assert len(case.openings) == 3
    g = ops.new_opening(d, "inlet", "xmin")
    assert g["name"] == "inlet_2"
    assert geo.Rect.from_center(g["center"], g["size"]).overlap_area(
        geo.Rect.from_center(a["center"], a["size"])) == 0


@pytest.mark.parametrize("mode", ["flow", "velocity", "fan", "free"])
def test_drive_modes_stay_valid(mode):
    d = _data()
    inlet = d["openings"][0]
    ops.set_drive(inlet, mode)
    assert ops.drive_mode(inlet) == mode
    Case.model_validate(d)


def test_drive_switch_keeps_the_flow_rate():
    o = {"kind": "inlet", "size": [0.1, 0.05], "flow_rate": 0.005}
    ops.set_drive(o, "velocity")
    assert o["velocity"] == pytest.approx(1.0) and o["flow_rate"] is None
    ops.set_drive(o, "flow")
    assert o["flow_rate"] == pytest.approx(0.005)


def test_fan_spec_and_kind():
    d = _data()
    fan = d["openings"][0]
    ops.set_fan_spec(fan, "flow")
    assert fan["fan"]["flow_rate"] > 0 and fan["fan"]["curve"] is None
    ops.set_fan_spec(fan, "curve")
    assert fan["fan"]["curve"] and fan["fan"]["flow_rate"] is None
    vent = d["openings"][1]
    ops.set_drive(vent, "fan")  # exhaust fan on the outlet
    Case.model_validate(d)
    ops.set_kind(vent, "inlet")
    assert vent["kind"] == "inlet"


def test_walls_override():
    d = _data()
    d.pop("walls", None)
    ops.set_face_override(d, "zmin", {"mode": "adiabatic"})
    ops.walls(d)["mode"] = "exchange"
    case = Case.model_validate(d)
    assert case.walls.for_face("zmin").mode.value == "adiabatic"
    assert case.walls.for_face("xmin").mode.value == "exchange"
    assert ops.wall_for_face(d, "xmin")["mode"] == "exchange"


# ---------------------------------------------------------------- checks (no Qt)
def _messages(alerts):
    return [a.message for a in alerts]


def test_checks_clean_case():
    d = _data()
    assert air_alerts(d, Case.model_validate(d)) == []


def test_check_overflow_and_overlap():
    d = _data()
    d["openings"][1]["center"] = [0.11, 0.03]  # 80 mm wide outlet near the edge of 120 mm
    alerts = air_alerts(d)
    assert any("dépasse" in m and "vent" in m for m in _messages(alerts))
    assert alerts[0].target == "opening:vent" and alerts[0].level == "error"
    d = _data()
    d["openings"][1]["face"] = "xmin"
    alerts = air_alerts(d)
    assert any("se recouvrent" in m for m in _messages(alerts))


def test_check_missing_outlet_and_inlet():
    d = _data()
    d["openings"] = [d["openings"][0]]
    msgs = _messages(air_alerts(d))
    assert any(m.startswith("Aucune sortie d’air") for m in msgs)
    d["openings"] = []
    msgs = _messages(air_alerts(d))
    assert any(m.startswith("Aucune entrée d’air") for m in msgs)


def test_check_blocked_and_slow():
    d = _data()
    # a big part against the outlet's face covers it
    d["components"].append({"name": "psu", "material": "steel", "power": 0.0,
                            "box": {"origin": {"x": 0.15, "y": 0.0, "z": 0.0},
                                    "size": {"x": 0.05, "y": 0.12, "z": 0.06}}})
    msgs = _messages(air_alerts(d))
    assert any("bouchée" in m and "psu" in m for m in msgs)
    d = _data()
    d["openings"][1]["grille"] = {"open_area": 0.05}
    assert any("grille ouverte à 5 %" in m for m in _messages(air_alerts(d)))


def test_check_stalled_fan():
    d = _data()
    d["openings"][1]["grille"] = {"open_area": 0.02}  # nearly closed outlet
    case = Case.model_validate(d)
    assert any("presque bloqué" in m for m in _messages(air_alerts(d, case)))


# ---------------------------------------------------------------- UI
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)


@pytest.fixture
def win(qapp, tmp_path, settings):
    from pathlib import Path

    from ui_support import make_window

    w = make_window(tmp_path, settings,
                    case=Case.load(Path(__file__).parent.parent / FAN_BOX),
                    preview_fn=lambda c: {comp.name: 30.0 + i for i, comp in
                                          enumerate(c.components)})
    w.goto_tab("air")
    return w


def _tab(win):
    return win.tabs["air"]


def test_tab_shortcuts(win):
    keys = {a.shortcut().toString() for a in _tab(win).actions()}
    assert {"V", "I", "O", "N", "Del"} <= keys
    assert "F" not in keys  # F is "Cadrer"


def test_click_face_then_tool_creates_opening(win, qapp):
    tab = _tab(win)
    tab.use_tool("outlet")
    assert tab.armed == "outlet"
    win.viewport.scene.pick_name("face_zmax")  # what a click on the top face does
    o = win.project.get("openings", 2)
    assert o["kind"] == "outlet" and o["face"] == "zmax"
    assert win.selection.keys == [f"opening:{o['name']}"]
    assert tab.armed is None
    assert tab.stack.currentWidget() is tab.opening_inspector
    win.project.undo_stack.undo()
    assert len(win.project.get("openings")) == 2


def test_face_selected_opens_face_editor_and_places_at_click(win):
    tab = _tab(win)
    win.selection.set(["face:ymin"])
    assert tab.stack.currentWidget() is tab.face_inspector
    assert tab.face_inspector.editor.face == "ymin"
    tab.use_tool("fan")  # face editor open: wait for a click in it
    assert tab.armed == "fan"
    tab.face_inspector.editor.create_requested.emit((0.05, 0.03))
    o = win.project.get("openings", 2)
    assert o["face"] == "ymin" and o["fan"] is not None
    assert o["center"] == pytest.approx([0.05, 0.03])


def test_tool_with_opening_selected_adds_on_its_face(win):
    tab = _tab(win)
    win.selection.set(["opening:vent"])
    tab.use_tool("inlet")
    o = win.project.get("openings", 2)
    assert o["face"] == "xmax" and o["kind"] == "inlet"


def test_drag_in_face_editor_is_one_undo_step(win):
    tab = _tab(win)
    win.selection.set(["opening:vent"])
    ed = tab.opening_inspector.editor
    ed.resize(320, 240)
    from airconstruct.ui.tabs.air.geometry import opening_rect

    r = opening_rect(win.project.get("openings", 1))
    ed._drag = {"name": "vent", "corner": None, "start": r.center, "rect": r}
    n = win.project.undo_stack.count()
    for du in (0.005, 0.01, 0.018):
        center, size = ed.drag_to((r.center[0] + du, r.center[1]))
        ed.geometry_edited.emit("vent", center, size)
    ed._drag = None
    assert win.project.undo_stack.count() == n + 1
    c = win.project.get("openings", 1, "center")
    assert c[0] == pytest.approx(0.08)  # snapped to the right edge (120 - 80/2 mm)
    # resize from the top right corner, snapped to the top edge
    r = opening_rect(win.project.get("openings", 1))
    ed._drag = {"name": "vent", "corner": "tr", "start": (r.u1, r.v1), "rect": r}
    center, size = ed.drag_to((r.u1, 0.0595))
    assert r.v0 + size[1] == pytest.approx(0.06)


def test_opening_inspector_edits(win):
    tab = _tab(win)
    win.selection.set(["opening:vent"])
    insp = tab.opening_inspector
    insp.su.setValue(60.0)  # mm
    assert win.project.get("openings", 1, "size")[0] == pytest.approx(0.06)
    insp.show_page("grille")
    insp.open_area.setValue(70)
    assert win.project.get("openings", 1, "grille", "open_area") == pytest.approx(0.7)
    assert "K ≈" in insp.grille_info.text()
    insp.name_edit.setText("exhaust")
    insp._rename()
    assert win.project.get("openings", 1, "name") == "exhaust"
    assert win.selection.keys == ["opening:exhaust"]
    insp.name_edit.setText("fan")  # taken
    insp._rename()
    assert win.project.get("openings", 1, "name") == "exhaust"


def test_inlet_flow_and_temperature(win):
    tab = _tab(win)
    win.selection.set(["opening:fan"])
    insp = tab.opening_inspector
    i = insp.mode.findData("flow")
    insp.mode.setCurrentIndex(i)
    insp.mode.activated.emit(i)
    assert win.project.get("openings", 0, "fan") is None
    insp.flow.setValue(36.0)  # m3/h
    assert win.project.get("openings", 0, "flow_rate") == pytest.approx(0.01)
    insp.t_ambient.setChecked(False)
    insp.temperature.setValue(40.0)
    assert win.project.get("openings", 0, "temperature") == pytest.approx(40.0)
    assert win.project.case is not None


def test_velocity_mode_only_in_expert(win):
    tab = _tab(win)
    win.selection.set(["opening:fan"])
    insp = tab.opening_inspector
    assert insp.mode.findData("velocity") < 0
    win.ctx.expert.set(True)
    assert insp.mode.findData("velocity") >= 0


def test_fan_curve_and_operating_point(win):
    tab = _tab(win)
    win.selection.set(["opening:fan"])
    insp = tab.opening_inspector
    insp.show_page("fan")
    assert insp.curve.rowCount() == 4
    s = insp.chart.series
    assert s["fan"] and s["system"] and s["point"]
    q, dp = s["point"]
    assert 0 < q < 39.6 and dp > 0
    insp.curve.item(2, 1).setText("12")  # a stronger fan around the operating point
    assert win.project.get("openings", 0, "fan", "curve")[2][1] == pytest.approx(12.0)
    assert insp.chart.series["point"][0] > q
    insp._add_point()
    assert len(win.project.get("openings", 0, "fan", "curve")) == 5
    assert win.project.case is not None
    insp.fan_spec.changed.emit("flow")
    assert win.project.get("openings", 0, "fan", "flow_rate") > 0
    insp.fan_flow.setValue(20.0)
    assert win.project.get("openings", 0, "fan", "flow_rate") == pytest.approx(20 / 3600)


def test_walls_inspector(win):
    tab = _tab(win)
    win.selection.set(["walls"])
    assert tab.stack.currentWidget() is tab.walls_inspector
    form = tab.walls_inspector.common
    form.mode.setCurrentIndex(form.mode.findData("exchange"))
    form.thickness.setValue(2.0)
    case = win.project.case
    assert case.walls.mode.value == "exchange"
    assert case.walls.thickness == pytest.approx(0.002)
    combo = tab.walls_inspector.face_combos["zmin"]
    combo.setCurrentIndex(combo.findData("adiabatic"))
    combo.activated.emit(combo.currentIndex())
    assert win.project.case.walls.for_face("zmin").mode.value == "adiabatic"
    assert "1 face à part" in tab.tree._items["walls"].text(1)


def test_face_wall_custom(win):
    tab = _tab(win)
    win.selection.set(["face:zmax"])
    fi = tab.face_inspector
    fi.choice.setCurrentIndex(fi.choice.findData("custom"))
    fi.choice.activated.emit(fi.choice.currentIndex())
    assert fi.wall.isVisible() or not win.isVisible()
    fi.wall.h.setValue(25.0)
    w = win.project.case.walls.for_face("zmax")
    assert w.mode.value == "exchange" and w.h_external == pytest.approx(25.0)


def test_alerts_badge_and_dedup(win):
    win.project.edit("x", lambda d: ops.delete_opening(d, "vent"))
    msgs = [a.message for a in win.alerts.for_tab("air")]
    assert msgs.count("Aucune sortie d’air : ajoutez une sortie ou un ventilateur "
                      "d’extraction.") == 1
    assert win.tab_badge("air") != ""


def test_overflow_alert_targets_opening(win):
    win.project.edit("x", lambda d: d["openings"][1].__setitem__("size", [0.3, 0.04]))
    alerts = [a for a in win.alerts.for_tab("air") if "dépasse" in a.message]
    assert alerts and alerts[0].target == "opening:vent"


def test_quick_estimate_lists_every_part(win, qapp):
    tab = _tab(win)
    tab.estimate.update_now()
    assert set(tab.estimate.values) == {"board", "chip"}
    assert "m³/h" in tab.estimate.flow.text()
    assert "fan :" in tab.estimate.flow.text()


def test_delete_and_grille_toggle(win):
    tab = _tab(win)
    win.selection.set(["opening:fan"])
    tab.toggle_grille()
    assert win.project.get("openings", 0, "grille") is None
    tab.toggle_grille()
    assert win.project.get("openings", 0, "grille")["open_area"] == pytest.approx(0.5)
    del_action = next(a for a in tab.actions() if "supprimer" in a.text())
    del_action.setEnabled(True)
    del_action.trigger()
    assert [o["name"] for o in win.project.get("openings")] == ["vent"]
    assert win.selection.keys == []


def test_scene_has_clickable_faces(win):
    tab = _tab(win)
    items = tab.scene()
    keys = {i.key for i in items}
    assert {f"face:{f}" for f in geo.FACES} <= keys
    assert any(i.name == "fan_fan" for i in items)  # the fan ring
    assert any(i.name == "vent_grille" for i in items)
