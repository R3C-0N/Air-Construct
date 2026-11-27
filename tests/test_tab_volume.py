"""Volume d'air tab: fineness table, refinements and their cost, mesh checks and
fixes, the real-mesh cut."""

from __future__ import annotations

import itertools

import pytest
from ui_support import EXAMPLE, make_window

from airconstruct.case import Case
from airconstruct.mesh import plan_mesh
from airconstruct.ui.tabs.volume import meshinfo as mi


def est(case: Case) -> float:  # deterministic: 1 ms per cell
    return plan_mesh(case).n_cells * 1e-3


@pytest.fixture
def case():
    return Case.load(EXAMPLE)


# ------------------------------------------------------------------ meshinfo
def test_level_rows_grow_with_fineness(case):
    rows = mi.level_rows(case, est)
    assert [r.level for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0].name == "Très grossier"
    cells = [r.cells for r in rows]
    assert cells == sorted(cells) and len(set(cells)) == 5
    assert rows[1].cells == plan_mesh(case).n_cells  # the example is level 2


def test_refinement_cost_is_positive_and_counted_against_no_refinement(case):
    refined = mi.with_changes(case, lambda d: d.__setitem__(
        "refinements", [mi.default_refinement("cpu")]))
    cost = mi.refinement_cost(refined, "cpu", est)
    assert cost.cells == plan_mesh(refined).n_cells - plan_mesh(case).n_cells > 0
    assert cost.seconds == pytest.approx(cost.cells * 1e-3)
    assert mi.fmt_cost(cost).startswith("+") and "mailles" in mi.fmt_cost(cost)


def test_formatting():
    assert mi.fmt_cells(812) == "812"
    assert mi.fmt_cells(8200) == "8,2 k"
    assert mi.fmt_cells(110_000) == "110 k"
    assert mi.fmt_cells(1_250_000) == "1,2 M"
    assert mi.fmt_duration(42) == "42 s"
    assert mi.fmt_duration(12 * 60) == "12 min"
    assert mi.fmt_duration(80 * 60) == "1 h 20"
    assert mi.fmt_cost(mi.Cost(6000, 120)) == "+6 k mailles, +2 min"


def test_example_passes_every_check(case):
    checks = mi.checks(case, est)
    assert all(c.ok for c in checks), checks
    titles = [c.title for c in checks]
    assert "Volume d’air continu" in titles and "Entrée et sortie détectées" in titles


def _thin_hot_case(case):
    def f(d):
        d["precision"] = {"level": 1}
        for c in d["components"]:
            if c["name"] == "ram":
                c["box"]["size"]["z"] = 0.002
    return mi.with_changes(case, f)


def test_thin_heat_source_warns_and_fix_resolves_it(case):
    thin = _thin_hot_case(case)
    n, axis = mi.cells_across(thin, "ram")
    assert n < mi.MIN_CELLS_ACROSS and axis == 2
    bad = [c for c in mi.checks(thin, est) if not c.ok]
    ram = next(c for c in bad if c.target == "component:ram")
    assert ram.level == "warning" and ram.fix == "Raffiner ram" and ram.fix_cost.startswith("+")
    data = thin.model_dump(mode="json")
    mi.apply_fix(data, thin, ram.fix_id)
    fixed = Case(**data)
    assert mi.cells_across(fixed, "ram")[0] >= mi.MIN_CELLS_ACROSS
    assert not [c for c in mi.checks(fixed) if not c.ok and c.target == "component:ram"]


def test_blocked_air_path_is_an_error(case):
    def wall(d):  # a full-height plate across x cuts inlet from outlet
        E = d["enclosure"]
        d["components"].append({"name": "wall", "material": "aluminium",
                                "box": {"origin": {"x": 0.1, "y": 0.0, "z": 0.0},
                                        "size": {"x": 0.005, "y": E["y"], "z": E["z"]}}})
    cut = mi.with_changes(case, wall)
    assert mi.air_paths(cut)["reached_outlets"] == []
    bad = [c for c in mi.checks(cut) if not c.ok]
    assert any(c.title == "Volume d’air coupé" and c.level == "error" for c in bad)


def test_blocked_opening_and_unmeshed_part(case):
    def f(d):
        d["components"].append({"name": "plug", "material": "aluminium",
                                "box": {"origin": {"x": 0.0, "y": 0.0, "z": 0.0},
                                        "size": {"x": 0.01, "y": 0.12, "z": 0.06}}})
        d["components"].append({"name": "inner", "material": "aluminium",
                                "box": {"origin": {"x": 0.07, "y": 0.05, "z": 0.008},
                                        "size": {"x": 0.005, "y": 0.005, "z": 0.002}}})
    c = mi.with_changes(case, f)
    titles = {ch.title: ch for ch in mi.checks(c) if not ch.ok}
    assert titles["inlet bouchée par une pièce"].target == "opening:inlet"
    assert titles["inner non maillée"].target == "component:inner"


def test_node_coords_match_the_plan(case):
    refined = mi.with_changes(case, lambda d: d.__setitem__(
        "refinements", [mi.default_refinement("cpu")]))
    for g in mi.plan(refined).axes:
        xs = mi.node_coords(g)
        assert len(xs) == g.n + 1
        assert xs[0] == 0.0 and xs[-1] == pytest.approx(g.breakpoints[-1])
        assert all(b > a for a, b in itertools.pairwise(xs))
        for bp in g.breakpoints:  # every part edge is a node
            assert min(abs(x - bp) for x in xs) < 1e-9


def test_cut_grid_marks_solid_cells(case):
    p = mi.plan(case)
    cpu = next(c for c in case.components if c.name == "cpu")
    z = (cpu.box.min[2] + cpu.box.max[2]) / 2
    grid = mi.cut_grid(case, 2, z, p)
    assert grid.n_cells == p.divisions[0] * p.divisions[1]
    assert grid.cell_data["solid"].sum() > 0
    assert grid.cell_data["size_mm"].min() > 0
    empty = mi.cut_grid(case, 2, case.enclosure.z - 1e-4, p)  # above every part
    assert empty.cell_data["solid"].sum() == 0


# ------------------------------------------------------------------ the tab
@pytest.fixture
def win(qapp, tmp_path, settings):
    w = make_window(tmp_path, settings, estimate_fn=est)
    w.goto_tab("volume")
    yield w
    w.close()


def test_table_shows_levels_and_picks_one(win):
    tab = win.tabs["volume"]
    assert tab.table.rowCount() == 5
    assert tab.table.item(0, 0).text() == "Très grossier"
    assert tab.table.item(1, 1).text() == mi.fmt_cells(plan_mesh(win.project.case).n_cells)
    assert tab.table.selectionModel().selectedRows()[0].row() == 1
    tab.set_level(4)
    assert win.project.get("precision", "level") == 4
    assert tab.table.selectionModel().selectedRows()[0].row() == 3
    assert "Fin" in tab.summary.text()
    win.project.undo_stack.undo()
    assert win.project.get("precision", "level") == 2


def test_expert_slider_sets_a_free_cell_size(win):
    tab = win.tabs["volume"]
    assert tab.expert_box.isHidden()
    win.expert.set(True)
    assert not tab.expert_box.isHidden()
    tab.set_cell_size(0.008)
    assert win.project.get("precision", "cell_size") == pytest.approx(0.008)
    assert not tab.table.selectionModel().selectedRows()
    assert "taille libre" in tab.summary.text()
    assert not tab.levels_button.isHidden()
    tab.levels_button.click()
    assert win.project.get("precision", "cell_size") is None


def test_add_change_and_remove_a_refinement(win):
    tab = win.tabs["volume"]
    assert not tab.ref_empty.isHidden()
    win.selection.set(["component:cpu"])
    assert tab.refine_sel_button.isEnabled()
    tab.refine_selection()
    assert [r["target"] for r in win.project.get("refinements")] == ["cpu"]
    w = tab._ref_widgets["cpu"]
    assert w["cost"].text().startswith("+") and "mailles" in w["cost"].text()
    assert not tab.refine_sel_button.isEnabled()  # cpu is already refined
    w["factor"].setValue(4)
    assert win.project.get("refinements")[0]["factor"] == 4
    tab.remove_refinement("cpu")
    assert win.project.get("refinements") == []
    assert not tab.ref_empty.isHidden()


def test_add_menu_lists_parts_with_their_cost(win):
    tab = win.tabs["volume"]
    win.selection.set(["component:ram"])
    tab._fill_add_menu()
    texts = [a.text() for a in tab.add_menu.actions()]
    assert texts[0].startswith("ram") and "mailles" in texts[0]  # selection first
    tab.add_menu.actions()[0].trigger()
    assert win.project.get("refinements")[0]["target"] == "ram"


def test_alert_fix_and_show_part(win):
    tab = win.tabs["volume"]

    def thin(d):
        d["precision"] = {"level": 1}
        for c in d["components"]:
            if c["name"] == "ram":
                c["box"]["size"]["z"] = 0.002
    win.project.edit("thin ram", thin)
    assert any(a.target == "component:ram" for a in win.alerts.for_tab("volume"))
    assert win.tab_badge("volume")
    row = next(r for r in tab.check_rows if r["check"].target == "component:ram")
    assert "alerte" in tab.checks_pill.text()
    row["show"].click()
    assert win.selection.keys == ["component:ram"]
    assert tab.cut_axis != 2  # the cut shows ram's thickness (z)
    assert "ram" in tab.cut_info.text()
    row["fix"].click()
    assert win.project.get("refinements")[0]["target"] == "ram"
    assert not [a for a in win.alerts.for_tab("volume") if a.target == "component:ram"]
    assert not [r for r in tab.check_rows if r["check"].target == "component:ram"]


def test_scene_has_the_mesh_cut(win):
    tab = win.tabs["volume"]
    names = [i.name for i in tab.scene()]
    assert "mesh_cut" in names and "cpu" in names
    cut = next(i for i in tab.scene() if i.name == "mesh_cut")
    assert cut.scalars == "solid"
    tab.set_color_by_size(True)
    assert next(i for i in tab.scene() if i.name == "mesh_cut").scalars == "size_mm"
    tab.set_cut(0, 0.05)
    assert tab.cut_axis == 0 and tab.cut_pos == pytest.approx(0.05)
    assert tab.axis_buttons[0].isChecked()
    tab.set_show_mesh(False)
    assert "mesh_cut" not in [i.name for i in tab.scene()]
    assert not tab.mesh_button.isChecked()


def test_invalid_case_keeps_last_view_and_disables_edits(win):
    tab = win.tabs["volume"]
    win.project.edit("no outlet", lambda d: d.__setitem__(
        "openings", [o for o in d["openings"] if o["kind"] != "outlet"]))
    assert win.project.case is None
    assert not tab.table.isEnabled()
    assert tab.scene() is not None
