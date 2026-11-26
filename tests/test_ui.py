"""UI tests: offscreen Qt, 3D view disabled, runner/results mocked."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from ui_support import (
    EXAMPLE,
    SHIPPED_EXAMPLE,
    fake_run,
    fake_summary,
    make_window,
    run_and_wait,
    wait_until,
)

from airconstruct.case import Case
from airconstruct.runner import RunResult
from airconstruct.ui.form import CaseForm, FormError, format_duration
from airconstruct.ui.project import Project


def test_format_duration():
    assert format_duration(42) == "42 s"
    assert format_duration(600) == "10 min"
    assert format_duration(7500) == "2 h 05 min"


def _close(a: Case, b: Case) -> bool:
    assert a.name == b.name
    assert a.enclosure.x == pytest.approx(b.enclosure.x)
    assert [c.name for c in a.components] == [c.name for c in b.components]
    for ca, cb in zip(a.components, b.components):
        assert ca.material == cb.material
        assert ca.power == pytest.approx(cb.power)
        assert ca.box.min == pytest.approx(cb.box.min)
        assert ca.box.max == pytest.approx(cb.box.max)
    for oa, ob in zip(a.openings, b.openings):
        assert oa.kind == ob.kind and oa.face == ob.face
        if oa.kind == "inlet":
            assert oa.normal_velocity() == pytest.approx(ob.normal_velocity())
    assert a.precision == b.precision
    assert a.solver == b.solver
    return True


def test_form_roundtrip_values(qapp):
    case = Case.load(EXAMPLE)
    form = CaseForm()
    form.set_case(case)
    assert _close(form.to_case(), case)


def test_form_roundtrip_shipped_example(qapp):
    case = Case.load(SHIPPED_EXAMPLE)
    form = CaseForm()
    form.set_case(case)
    assert _close(form.to_case(), case)


def test_form_bad_number(qapp):
    form = CaseForm()
    form.set_case(Case.load(EXAMPLE))
    form.components.item(0, 8).setText("abc")
    with pytest.raises(FormError):
        form.to_case()


def test_form_add_rows_and_precision(qapp):
    form = CaseForm()
    form.set_case(Case.load(EXAMPLE))
    n = form.components.rowCount()
    form._add_component_clicked()
    form._add_opening_clicked()
    form.precision_slider.setValue(5)
    c = form.to_case()
    assert len(c.components) == n + 1
    assert c.components[-1].name == "part1"
    assert len(c.openings) == 3
    assert c.precision.level == 5


def test_geometry_items(qapp):
    pytest.importorskip("pyvista")
    from airconstruct.ui.view import geometry_items, opening_bounds

    case = Case.load(EXAMPLE)
    items = geometry_items(case)
    names = [i.name for i in items]
    assert names[0] == "enclosure"
    assert set(case.solid_regions) <= set(names)
    b = opening_bounds(case, case.openings[1])  # outlet on xmax
    assert b[0] < case.enclosure.x < b[1]
    assert b[2] == pytest.approx(0.02) and b[3] == pytest.approx(0.10)


def test_result_items(qapp):
    pv = pytest.importorskip("pyvista")
    import numpy as np

    from airconstruct.ui.view import result_items, slice_height

    case = Case.load(EXAMPLE)
    air = pv.ImageData(dimensions=(11, 7, 5), spacing=(0.02, 0.02, 0.015)).cast_to_unstructured_grid()
    air.cell_data["T"] = np.linspace(298.15, 320.0, air.n_cells)
    air.cell_data["U"] = np.ones((air.n_cells, 3))
    cpu = pv.Box((0.06, 0.09, 0.04, 0.07, 0.007, 0.013)).cast_to_unstructured_grid()
    cpu.cell_data["T"] = np.full(cpu.n_cells, 340.0)
    assert slice_height(case) == pytest.approx(0.010)  # through the cpu
    items = result_items(case, {"air": air, "cpu": cpu}, "T")
    names = [i.name for i in items]
    assert "air_slice" in names and "cpu" in names
    cpu_item = next(i for i in items if i.name == "cpu")
    assert cpu_item.mesh.cell_data["T_display"][0] == pytest.approx(66.85)
    u = result_items(case, {"air": air}, "U")
    assert any(i.name == "air_slice" for i in u)


def test_window_run_with_mocks(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings, run_fn=fake_run, summarize_fn=fake_summary,
                      load_regions_fn=lambda d: {}, estimate_fn=lambda c: 90.0)
    sim, res, model = win.tabs["simulation"], win.tabs["results"], win.tabs["model"]
    assert sim.run_button.isEnabled()
    assert "2 min" in model.form.estimate_label.text()
    outcome = run_and_wait(qapp, win)
    assert outcome.success
    assert "Convergé" in sim.progress.format()
    assert res.results.table.rowCount() == 4
    assert res.results.table.item(0, 0).text() == "cpu"  # hottest first
    assert "fake solver start" in sim.log.toPlainText()
    assert sim.run_button.isEnabled() and model.form.isEnabled()
    assert not win.project.stale and res.banner.isHidden()
    win.close()


def test_window_without_openfoam(qapp, tmp_path, settings, monkeypatch):
    """Default runner path: a missing OpenFOAM is reported, not crashed on."""
    from airconstruct import runner

    class NoFoam(runner.Backend):
        def available(self):
            return False, "OpenFOAM not found (test)"

    monkeypatch.setattr(runner, "detect_backend", lambda: NoFoam())
    win = make_window(tmp_path, settings)
    outcome = run_and_wait(qapp, win)
    assert not outcome.success
    assert outcome.errors == ["OpenFOAM is not available: OpenFOAM not found (test)"]
    assert "OpenFOAM not found" in win.tabs["results"].results.headline.text()
    assert "Échoué" in win.tabs["simulation"].progress.format()
    win.close()


def test_window_default_runner_gets_backend(qapp, tmp_path, settings, monkeypatch):
    from airconstruct import runner

    class FakeFoam(runner.Backend):
        def available(self):
            return True, "OpenFOAM v2406 (test)"

    seen = {}

    def run_case(case, work_dir, backend=None, **kw):
        seen["backend"] = backend
        kw["on_log"]("--- phase energy: iterations 400..7400 (frozen flow)")
        return fake_run(case, work_dir, **kw)

    monkeypatch.setattr(runner, "detect_backend", lambda: FakeFoam())
    monkeypatch.setattr(runner, "run_case", run_case)
    win = make_window(tmp_path, settings, summarize_fn=fake_summary, load_regions_fn=lambda d: {})
    outcome = run_and_wait(qapp, win)
    assert isinstance(seen["backend"], FakeFoam)
    assert outcome.success
    assert "Using OpenFOAM v2406 (test)" in win.tabs["simulation"].log.toPlainText()
    win.close()


def test_quick_estimate_preview(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    res = win.tabs["results"]
    assert "Estimation rapide" in res.results.headline.text()
    assert res.results.table.rowCount() == 4
    assert res.results.table.item(0, 0).text() == "cpu"  # 15 W: hottest
    t_cpu = float(res.results.table.item(0, 2).text())
    assert 25.0 < t_cpu < 300.0
    win.close()


def test_phase_and_progress(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    r = win.runner
    r.run_case = win.project.case
    r._set_progress(0, 100)
    r._on_log("$ blockMesh")
    assert "Maillage (blockMesh)" in r.progress_text
    r._on_log("--- phase flow: iterations 0..400")
    assert r.progress_text.startswith("Calcul de l’écoulement d’air")
    r._on_residual(150, {"Ux": 1e-3})
    assert r.progress_max == win.project.case.solver.iterations
    assert r.progress_value == 150
    sim = win.tabs["simulation"]
    assert sim.progress.value() == 150
    win.close()


def test_window_invalid_case_disables_run(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    win.project.edit("supprimer la sortie", lambda d: d["openings"].pop(1))
    sim = win.tabs["simulation"]
    assert win.project.case is None
    assert not sim.run_button.isEnabled() and not win.run_action.isEnabled()
    assert not win.start_run()
    assert "outlet" in sim.blocked_label.text()
    assert [a.tab for a in win.alerts.alerts] == ["air"]
    assert win.tabs["air"]._badge.text() == "1"
    assert win.status.alerts_button.text() == "1 alerte"
    win.project.undo_stack.undo()
    assert win.project.case is not None and sim.run_button.isEnabled()
    assert win.status.alerts_button.text() == "✓ Aucune alerte"
    win.close()


def test_window_save_load(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    p = tmp_path / "saved.yaml"
    assert win.write_case(p)
    assert win.state_label.text() == "enregistré"
    assert win.load_case(p)
    assert _close(win.project.case, Case.load(EXAMPLE))
    assert _close(win.tabs["model"].form.to_case(), Case.load(EXAMPLE))
    assert win.recent_files()[0] == str(p.resolve())
    win.close()


def test_window_with_real_results_module(qapp, tmp_path, settings):
    """Mocked solver, real results.summarize / load_regions / write_report on a
    synthetic OpenFOAM case written by tests/foam_fixture.py."""
    pytest.importorskip("pyvista")
    import numpy as np
    from foam_fixture import write_case

    case = Case.load(EXAMPLE)
    case.components[0].box.origin.z = 0.004  # put parts on cell faces, as test_results
    for comp in case.components[1:]:
        comp.box.origin.z = 0.006
    case_dir = tmp_path / "runs" / case.name

    def T(x, region):
        return np.full(len(x), 350.0 if region == "cpu" else 300.0) + x[:, 0]

    def U(x):
        u = np.zeros_like(x)
        u[:, 0] = 1.0
        return u

    write_case(case, case_dir, (40, 24, 30), T=T, U=U,
               p=lambda x: 101325.0 + 10.0 * (1.0 - x[:, 0] / 0.2), times=("0", "100"))

    def run(case, work_dir, **kw):
        kw["on_residual"](100, {"h": 1e-5})
        return RunResult(case_dir=case_dir, success=True, converged=True, iterations=100,
                         wall_time_s=3.0)

    win = make_window(tmp_path, settings, case=case, run_fn=run, open_report=False)
    outcome = run_and_wait(qapp, win, 60)
    assert outcome.success, outcome.errors
    res = win.tabs["results"]
    assert res.results.table.item(0, 0).text() == "cpu"
    assert float(res.results.table.item(0, 2).text()) == pytest.approx(76.9, abs=0.5)
    assert set(outcome.regions) >= {"air", "cpu"}
    assert res.field_combo.isEnabled()
    win.goto_tab("results")
    assert any(i.name == "air_slice" for i in win.viewport.items)  # result fields drawn
    res.geometry_button.setChecked(True)
    win.refresh_scene()
    assert not any(i.name == "air_slice" for i in win.viewport.items)
    res.save_report()
    assert res.last_report is not None and res.last_report.exists()
    win.close()


@pytest.mark.openfoam
def test_window_real_openfoam_run(qapp, tmp_path, settings):
    """Run button -> real runner (OpenFOAM) -> real results, with a short iteration cap."""
    from airconstruct.runner import detect_backend

    ok, msg = detect_backend().available()
    if not ok:
        if os.environ.get("AIRCONSTRUCT_REQUIRE_OPENFOAM"):
            pytest.fail(msg)
        pytest.skip(msg)
    case = Case.load(SHIPPED_EXAMPLE)
    case.precision.level = 1
    case.solver.iterations = 300
    win = make_window(tmp_path, settings, case=case, open_report=False,
                      estimate_fn=None)
    outcome = run_and_wait(qapp, win, 1200)
    sim = win.tabs["simulation"]
    log = sim.log.toPlainText()
    assert outcome.success, "\n".join(outcome.errors) + "\n" + log[-3000:]
    # (the log widget keeps only the last 5000 lines, so check the result, not early lines)
    assert outcome.result.iterations > 0
    assert sim.progress.format().startswith(("Convergé", "Terminé"))
    assert outcome.summary is not None
    assert "air" in outcome.regions
    assert win.tabs["results"].results.table.rowCount() == len(case.components)
    win.tabs["results"].save_report()
    assert win.tabs["results"].last_report is not None
    assert win.tabs["results"].last_report.exists()
    win.close()


# --------------------------------------------------------------------- new shell


def test_shell_has_five_tabs_and_swaps_panels(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    titles = [win.tab_bar.tabText(i) for i in range(win.tab_bar.count())]
    assert titles == ["Modèle", "Air", "Volume d’air", "Simulation", "Résultats"]
    assert win.current_tab is win.tabs["model"]
    win.goto_tab("simulation")
    assert win.current_tab is win.tabs["simulation"]
    assert win.right_stack.currentWidget() is win.tabs["simulation"].right_panel()
    win.goto_tab("volume")  # no right panel: the stack is hidden
    assert not win.right_stack.isVisibleTo(win)
    win.goto_tab("results")
    assert win.viewport.tab_toolbars.currentWidget() is win.tabs["results"].viewport_toolbar()
    win.close()


def test_undo_redo_through_the_legacy_form(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    form = win.tabs["model"].form
    assert not win.project.dirty
    form.ambient_spin.setValue(35.0)
    win.tabs["model"].binding.push()
    assert win.project.case.ambient_temperature == 35.0
    assert win.project.dirty and win.state_label.text() == "modifié"
    assert win.undo_action.isEnabled()
    win.undo_action.trigger()
    assert win.project.case.ambient_temperature == 25.0
    assert form.ambient_spin.value() == 25.0  # form follows the project
    assert not win.project.dirty
    win.redo_action.trigger()
    assert form.ambient_spin.value() == 35.0
    win.close()


def test_stale_results_banner(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings, run_fn=fake_run, summarize_fn=fake_summary,
                      load_regions_fn=lambda d: {})
    res = win.tabs["results"]
    run_and_wait(qapp, win)
    assert not win.project.stale
    win.project.edit("UI only", lambda d: d.setdefault("ui", {}).update(hidden=["cpu"]))
    assert not win.project.stale  # ui metadata does not change the physics
    win.project.edit("Puissance", lambda d: d["components"][0].update(power=20.0))
    assert win.project.stale and not res.banner.isHidden()
    res.dismiss_stale()
    assert res.banner.isHidden()
    win.project.undo_stack.undo()  # back to the simulated model
    assert not win.project.stale
    win.project.undo_stack.redo()
    assert win.project.stale and not res.banner.isHidden()
    res.banner.rerun_button.click()
    assert wait_until(qapp, lambda: not win.runner.running and win.project.results is not None)
    assert not win.project.stale
    win.close()


def test_selection_tree_viewport_and_alerts(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    tree = win.tabs["model"].tree
    # 3D pick -> selection -> tree
    win.viewport.scene.pick_name("cpu")
    assert win.selection.keys == ["component:cpu"]
    assert tree._items["component:cpu"].isSelected()
    win.viewport.scene.pick_name("inlet", additive=True)
    assert win.selection.keys == ["component:cpu", "opening:inlet"]
    # highlighted in the viewport
    hl = {i.key for i in win.viewport.scene.last_items if i.kwargs.get("edge_color") == "#1D4ED8"}
    assert hl == {"component:cpu", "opening:inlet"}
    # tree -> selection
    tree.clearSelection()
    tree._items["enclosure"].setSelected(True)
    assert win.selection.keys == ["enclosure"]
    # click in empty space clears
    win.viewport.scene.pick_name(None)
    assert win.selection.keys == []
    # deleting the selected part drops it from the selection
    win.selection.set(["component:cpu"])
    win.project.edit("delete", lambda d: d["components"].pop(1))  # cpu
    assert win.selection.keys == []
    # clicking an alert opens its tab and selects the object at fault
    win.project.edit("move", lambda d: d["components"][0]["box"]["origin"].update(x=5.0))
    alert = win.alerts.alerts[0]
    assert alert.target == "component:" + win.project.get("components", 0, "name")
    win.goto_tab("results")
    win._on_alert_clicked(alert)
    assert win.current_tab is win.tabs["model"] and win.selection.keys == [alert.target]
    win.close()


def test_units_and_shortcuts(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    assert win.status.units_button.text() == "mm · °C ▾"
    assert win.units.fmt_length(0.2) == "200,0 mm"
    win.units.set_length("m")
    win.units.set_temperature("K")
    assert win.status.units_button.text() == "m · K ▾"
    assert win.units.fmt_temperature(25.0) == "298,1 K"
    assert "0.2 × 0.12" in win.tabs["model"].tree._items["enclosure"].text(0)
    rows = dict(win.status.shortcut_rows())
    assert rows["Lancer le calcul"] == "Ctrl+R"
    assert "Ctrl+Z" in rows["Annuler"]
    dlg = win.status.show_shortcuts()
    dlg.close()
    win.close()
    # units and expert mode are remembered
    win2 = make_window(tmp_path, settings)
    assert win2.units.length == "m"
    win2.close()


def test_invalid_project_can_be_saved_and_reopened(qapp, tmp_path, settings):
    win = make_window(tmp_path, settings)
    win.project.edit("no outlet", lambda d: d["openings"].pop(1))
    p = tmp_path / "wip.yaml"
    assert win.write_case(p)
    assert "pas encore valide" in win.status.currentMessage()
    win.new_case()
    assert win.project.case is not None and win.project.path is None
    assert win.load_case(p)
    assert win.project.case is None and win.alerts.has_errors
    assert win.project.undo_stack.count() == 0
    win.close()


def test_project_model():
    pytest.importorskip("PySide6.QtCore")
    data = Case.load(EXAMPLE).model_dump(mode="json")
    p = Project(data)
    assert p.case is not None and not p.dirty
    assert not p.edit("noop", lambda d: None)
    # edits with one merge key, close in time, are one undo step
    p.edit("a", lambda d: d.update(ambient_temperature=30.0), merge_key="k")
    p.edit("b", lambda d: d.update(ambient_temperature=31.0), merge_key="k")
    assert p.undo_stack.count() == 1
    p.edit("c", lambda d: d.update(ambient_temperature=32.0))
    assert p.undo_stack.count() == 2
    p.undo_stack.undo()
    p.undo_stack.undo()
    assert p.get("ambient_temperature") == 25.0
    # the data given out is a copy
    d = p.data
    d["name"] = "changed"
    assert p.name != "changed"
    # unknown keys survive validation and saving
    p.edit("extra", lambda d: d.update(ui={"locked": ["board"]}))
    assert "ui" in yaml_roundtrip(p)
    assert p.case is not None


def yaml_roundtrip(p: Project) -> dict:
    import yaml

    return yaml.safe_load(p.to_yaml())


def test_highlight_items_and_selection_helpers():
    from airconstruct.ui.selection import Selection, existing_keys, parse_key
    from airconstruct.ui.view import SceneItem
    from airconstruct.ui.viewport import highlight_items

    items = [SceneItem("enclosure", None, "#555", style="wireframe", key="enclosure"),
             SceneItem("cpu", None, "#f00", opacity=0.45, key="component:cpu"),
             SceneItem("air_slice", None)]
    out = highlight_items(items, ["enclosure", "component:cpu"])
    assert out[0].color == "#1D4ED8" and out[0].kwargs["line_width"] == 3
    assert out[1].kwargs["edge_color"] == "#1D4ED8" and out[1].opacity == 0.8
    assert out[2] is items[2] and items[1].kwargs == {}
    assert parse_key("opening:inlet") == ("opening", "inlet")
    assert parse_key("enclosure") == ("enclosure", None)
    data = Case.load(EXAMPLE).model_dump(mode="json")
    assert "component:cpu" in existing_keys(data)
    s = Selection()
    s.select("component:cpu")
    s.select("component:cpu", additive=True)
    assert s.keys == []
    s.set(["component:cpu", "fan:f1"])
    s.rename("component:cpu", "component:gpu")
    s.prune(data)
    assert s.keys == ["fan:f1"]  # unknown kinds are left to their owner


def test_validation_alerts_mapping():
    from pydantic import ValidationError

    from airconstruct.ui.alerts import validation_alerts

    data = Case.load(EXAMPLE).model_dump(mode="json")
    data["components"][1]["power"] = "abc"
    with pytest.raises(ValidationError) as e:
        Case.model_validate(data)
    (a,) = validation_alerts(data, e.value)
    assert a.tab == "model" and a.target == "component:" + data["components"][1]["name"]
    data = Case.load(EXAMPLE).model_dump(mode="json")
    data["openings"][0]["size"] = [0.0]
    with pytest.raises(ValidationError) as e:
        Case.model_validate(data)
    assert {x.tab for x in validation_alerts(data, e.value)} == {"air"}
