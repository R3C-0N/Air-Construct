"""Mouse in the 3D view: click to select, drag parts and probes, place a probe by
clicking (v1.1.0). The VTK side (picking, rays) is checked by hand; here the plane
maths are pure functions and the tabs are driven through the viewport's drag and
click entry points, which the scene view calls with what it picked."""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from ui_support import make_window

from airconstruct.ui.tabs.model import geometry as geo
from airconstruct.ui.tabs.simulation import placement
from airconstruct.ui.view import Pick, drag_normal, near_box_edge, ray_plane

ENC = {"x": 0.2, "y": 0.12, "z": 0.06}
DATA = {
    "name": "t", "enclosure": ENC,
    "components": [
        {"name": "board", "material": "fr4",
         "box": {"origin": {"x": 0.01, "y": 0.01, "z": 0.005},
                 "size": {"x": 0.18, "y": 0.10, "z": 0.002}}},
        {"name": "cpu", "material": "silicon", "power": 8.0,
         "box": {"origin": {"x": 0.06, "y": 0.04, "z": 0.007},
                 "size": {"x": 0.03, "y": 0.03, "z": 0.003}}},
        {"name": "ram", "material": "silicon", "power": 2.0,
         "box": {"origin": {"x": 0.12, "y": 0.04, "z": 0.007},
                 "size": {"x": 0.02, "y": 0.03, "z": 0.003}}},
    ],
}


def data():
    return copy.deepcopy(DATA)


def origin(win, name):
    c = next(c for c in win.project.get("components") if c["name"] == name)
    return tuple(c["box"]["origin"][a] for a in "xyz")


# ----------------------------------------------------------------- plane maths
def test_drag_plane_faces_the_camera():
    assert drag_normal((0, 0, -1)) == (0.0, 0.0, 1.0)  # top view: slide on the floor
    assert drag_normal((0, 1, 0)) == (0.0, 1.0, 0.0)  # front view: x and height
    assert drag_normal((1, 0, 0)) == (1.0, 0.0, 0.0)  # side view
    assert drag_normal((-0.577, -0.577, -0.577)) == (0.0, 0.0, 1.0)  # iso: horizontal
    n = drag_normal((-0.6, -0.6, -0.5), vertical=True)  # Shift: up and down
    assert n[2] == 0.0 and abs(n[0] ** 2 + n[1] ** 2 - 1) < 1e-9
    assert drag_normal((0, 0, -1), vertical=True) == (0.0, 1.0, 0.0)  # from above


def test_ray_plane():
    assert ray_plane((0, 0, 1), (0, 0, -1), (5, 5, 0.25), (0, 0, 1)) == (0, 0, 0.25)
    assert ray_plane((0, 0, 1), (1, 0, 0), (0, 0, 0), (0, 0, 1)) is None  # parallel
    assert ray_plane((0, 0, 1), (0, 0, 1), (0, 0, 0), (0, 0, 1)) is None  # behind


def test_wireframe_counts_only_on_its_lines():
    b = (0, 0.2, 0, 0.12, 0, 0.06)
    assert near_box_edge(b, (0.1, 0.0005, 0.06), 0.002)  # on the top front edge
    assert not near_box_edge(b, (0.1, 0.06, 0.06), 0.002)  # middle of the top face


# ----------------------------------------------------------------- magnet
def test_magnet_sticks_to_a_neighbour_and_a_wall():
    d = data()
    tol = geo.magnet_tolerance(d)
    assert tol == pytest.approx(0.003)
    # cpu right face at 0.09; ram left face at 0.12: a 28 mm drag ends 2 mm short
    assert geo.magnet(d, ["cpu"], [0.028, 0, 0], tol) == pytest.approx([0.03, 0, 0])
    # far from everything: unchanged
    assert geo.magnet(d, ["cpu"], [0.01, 0, 0], tol) == pytest.approx([0.01, 0, 0])
    # against the back wall (y = 0.12): cpu top in y is 0.07
    assert geo.magnet(d, ["cpu"], [0, 0.048, 0], tol) == pytest.approx([0, 0.05, 0])
    # an axis not dragged is never changed
    assert geo.magnet(d, ["cpu"], [0.028, 0, 0], tol)[2] == 0
    # a part far away on another axis does not attract
    d["components"][2]["box"]["origin"]["y"] = 0.09
    assert geo.magnet(d, ["cpu"], [0.028, 0, 0], tol) == pytest.approx([0.028, 0, 0])


# ----------------------------------------------------------------- probe placement
def test_probe_on_a_part_sits_off_the_clicked_face():
    d = data()
    top = placement.on_part(d, "cpu", (0.075, 0.055, 0.010), (0, 0, 1), (0, 0, -1))
    assert top == pytest.approx({"x": 0.075, "y": 0.055, "z": 0.012})
    inside = placement.on_part(d, "cpu", (0.075, 0.055, 0.010), (0, 0, 1), (0, 0, -1),
                               inside=True)
    assert inside["z"] == pytest.approx(0.0085)  # half the 3 mm thickness
    side = placement.on_part(d, "cpu", (0.06, 0.055, 0.008), None, (1, 0, 0))
    assert side["x"] == pytest.approx(0.058)  # no normal: nearest face, towards the viewer
    assert placement.on_part(d, "nope", (0, 0, 0), None) is None


def test_probe_in_the_air_and_dragged():
    d = data()
    p = placement.in_air(d, (0.05, 0.03, 1.0), (0, 0, -1))  # from above: mid height
    assert p == pytest.approx({"x": 0.05, "y": 0.03, "z": 0.03})
    assert placement.in_air(d, (0.5, 0.03, 1.0), (0, 0, -1)) is None  # beside the box
    assert placement.dragged(d, (0.1, 0.06, 0.03), (0.01234, 0, 0)) == \
        pytest.approx([0.1125, 0.06, 0.03])
    assert placement.dragged(d, (0.1, 0.06, 0.03), (0.5, 0, 0))[0] < 0.2  # stays inside
    assert placement.dragged(d, (0.1, 0.06, 0.03), (0.01234, 0, 0), free=True)[0] == \
        pytest.approx(0.11234)


# ----------------------------------------------------------------- Modèle tab
def window(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    win.project.set_data("données", data())
    win.goto_tab("model")
    qapp.processEvents()
    return win


def test_click_selects_double_click_frames(qapp, tmp_path, settings):
    win = window(qapp, tmp_path, settings)
    vp = win.viewport
    vp.scene.click(Pick("component:cpu", (0.07, 0.05, 0.01), (0, 0, 1), (0, 0, -1)))
    assert win.selection.keys == ["component:cpu"]
    vp.scene.click(Pick("component:ram", (0.13, 0.05, 0.01), (0, 0, 1), (0, 0, -1),
                        additive=True))
    assert win.selection.keys == ["component:cpu", "component:ram"]
    framed = []
    vp.frame = lambda keys=None: framed.append(keys)
    vp.scene.click(Pick("component:ram", (0.13, 0.05, 0.01), (0, 0, 1), (0, 0, -1),
                        double=True))
    assert framed == [["component:ram"]]
    vp.scene.click(Pick(None, None, (0, 0, 1), (0, 0, -1)))  # nothing under the cursor
    assert win.selection.keys == []


def test_drag_a_part_moves_it_snapped_in_one_undo_step(qapp, tmp_path, settings):
    win = window(qapp, tmp_path, settings)
    vp = win.viewport
    assert vp.drag_start("component:cpu", (0.07, 0.05, 0.01))
    assert vp.scene.drag_keys == ["component:cpu"]
    assert vp.drag_move((0.0123, 0.0, 0.0)) == pytest.approx((0.01, 0.0, 0.0))  # 5 mm grid
    assert vp.drag_move((0.0123, 0.0, 0.0), free=True) == pytest.approx((0.0123, 0.0, 0.0))
    assert vp.drag_end((0.0123, -0.0081, 0.0))
    assert origin(win, "cpu") == pytest.approx((0.07, 0.03, 0.007))
    assert win.selection.keys == ["component:cpu"]  # what was dragged is selected
    assert win.project.undo_stack.undoText() == "Déplacer cpu"
    # stuck to ram: its left face is at 0.12, cpu's right face ends 2 mm short
    assert vp.drag_start("component:cpu", (0.08, 0.04, 0.01))
    assert vp.drag_end((0.018, 0.0, 0.0))
    assert origin(win, "cpu")[0] == pytest.approx(0.09)
    win.project.undo_stack.undo()
    win.project.undo_stack.undo()
    assert origin(win, "cpu") == pytest.approx((0.06, 0.04, 0.007))


def test_drag_moves_the_selection_and_groups(qapp, tmp_path, settings):
    win = window(qapp, tmp_path, settings)
    win.selection.set(["component:cpu", "component:ram"])
    vp = win.viewport
    assert vp.drag_start("component:cpu", (0.07, 0.05, 0.01))
    assert sorted(vp.scene.drag_keys) == ["component:cpu", "component:ram"]
    assert vp.drag_end((0.0, 0.01, 0.0))
    assert origin(win, "cpu")[1] == pytest.approx(0.05)
    assert origin(win, "ram")[1] == pytest.approx(0.05)
    assert win.project.undo_stack.undoText() == "Déplacer la sélection"
    # an unselected part is dragged alone and becomes the selection
    assert vp.drag_start("component:board", (0.1, 0.06, 0.007))
    assert vp.scene.drag_keys == ["component:board"]
    assert vp.drag_end((0.0, 0.0, 0.005))
    assert win.selection.keys == ["component:board"]


def test_locked_parts_enclosure_and_runs_turn_the_camera(qapp, tmp_path, settings):
    win = window(qapp, tmp_path, settings)
    vp = win.viewport
    assert not vp.drag_start("enclosure", (0.1, 0.0, 0.06))
    assert not vp.drag_start("opening:inlet", (0.0, 0.03, 0.03))
    win.project.edit("lock", lambda d: geo.set_flag(d, "locked", ["cpu"], True))
    assert not vp.drag_start("component:cpu", (0.07, 0.05, 0.01))
    # a press without moving: nothing changes, the click still selects
    assert vp.drag_start("component:ram", (0.13, 0.05, 0.01))
    assert not vp.drag_end((0.0, 0.0, 0.0))
    assert origin(win, "ram") == pytest.approx((0.12, 0.04, 0.007))


# ----------------------------------------------------------------- Simulation tab
def test_place_a_probe_by_clicking_then_drag_it(qapp, tmp_path, settings):
    win = window(qapp, tmp_path, settings)
    win.goto_tab("simulation")
    sim = win.tabs["simulation"]
    vp = win.viewport
    # without placing, a click selects as usual
    vp.scene.click(Pick("component:cpu", (0.075, 0.055, 0.01), (0, 0, 1), (0, 0, -1)))
    assert win.selection.keys == ["component:cpu"]
    assert sim.start_placing() and sim.probe_tool.isChecked()
    vp.scene.click(Pick("component:cpu", (0.075, 0.055, 0.01), (0.075, 0.055, 1),
                        (0, 0, -1), normal=(0, 0, 1)))
    probes = win.project.get("probes")
    assert [p["name"] for p in probes] == ["S1"]
    assert probes[0]["point"] == pytest.approx({"x": 0.075, "y": 0.055, "z": 0.012})
    assert win.selection.keys == ["probe:S1"] and not sim.placing
    # a click in the air of the box: mid-plane facing the camera
    sim.start_placing()
    vp.scene.click(Pick(None, None, (0.03, 0.1, 1.0), (0, 0, -1)))
    assert win.project.get("probes")[1]["point"] == pytest.approx(
        {"x": 0.03, "y": 0.1, "z": 0.03})
    # outside the box: nothing placed, still waiting; Esc cancels and keeps the selection
    sim.start_placing()
    vp.scene.click(Pick(None, None, (0.5, 0.1, 1.0), (0, 0, -1)))
    assert len(win.project.get("probes")) == 2 and sim.placing
    win.escape()
    assert not sim.placing and win.selection.keys == ["probe:S2"]
    win.escape()
    assert win.selection.keys == []
    # drag S1 sideways: half millimetres, one undo step
    assert not vp.drag_start("component:cpu", (0.075, 0.055, 0.01))  # parts stay put here
    assert vp.drag_start("probe:S1", (0.075, 0.055, 0.012))
    assert vp.drag_end((0.01234, 0.0, 0.0))
    assert win.project.get("probes")[0]["point"]["x"] == pytest.approx(0.0875)
    assert win.project.undo_stack.undoText() == "Déplacer la sonde S1"


def test_probe_tool_without_3d_places_near_the_selection(qapp, tmp_path, settings):
    win = window(qapp, tmp_path, settings)
    win.goto_tab("simulation")
    sim = win.tabs["simulation"]
    win.selection.set(["component:cpu"])
    sim.probe_tool_clicked()  # no 3D view in tests: placed right away, as before
    assert not sim.placing and not sim.probe_tool.isChecked()
    p = win.project.get("probes")[0]["point"]
    assert p == pytest.approx({"x": 0.075, "y": 0.055, "z": 0.012})


def test_probes_stay_put_during_a_run(qapp, tmp_path, settings):
    win = window(qapp, tmp_path, settings)
    win.goto_tab("simulation")
    sim = win.tabs["simulation"]
    sim.live_card.add_probe({"x": 0.1, "y": 0.06, "z": 0.03})
    win.project.set_locked(True, "calcul en cours")
    try:
        assert not sim.start_placing()
        assert not win.viewport.drag_start("probe:S1", (0.1, 0.06, 0.03))
    finally:
        win.project.set_locked(False)
