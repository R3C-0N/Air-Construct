"""Résultats tab: data helpers, filter pipeline, charts and the tab in the window."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from ui_support import EXAMPLE, fake_run, fake_summary, make_window, run_and_wait

from airconstruct.case import Case

pv = pytest.importorskip("pyvista")


# --------------------------------------------------------------------------- fixtures


def _grid(lo, hi, n):
    spacing = [(h - lo_) / (k - 1) for lo_, h, k in zip(lo, hi, n, strict=True)]
    return pv.ImageData(dimensions=n, spacing=spacing, origin=lo).cast_to_unstructured_grid()


def fake_regions(case: Case, hot: float = 350.0) -> dict:
    """Air with T rising along x and a uniform +x flow; cpu at a uniform temperature."""
    E = case.enclosure.as_tuple()
    air = _grid((0, 0, 0), E, (21, 13, 7))
    x = np.asarray(air.cell_centers().points)
    air.cell_data["T"] = 300.0 + 100.0 * x[:, 0]
    u = np.zeros_like(x)
    u[:, 0] = 1.0
    u[:, 2] = 0.1 * np.sin(30 * x[:, 0])
    air.cell_data["U"] = u
    c = next(c for c in case.components if c.name == "cpu")
    lo, hi = c.box.min, c.box.max
    cpu = _grid(tuple(lo), tuple(hi), (4, 4, 3))
    cpu.cell_data["T"] = np.full(cpu.n_cells, hot)
    return {"air": air, "cpu": cpu}


@pytest.fixture
def case():
    return Case.load(EXAMPLE)


# --------------------------------------------------------------------------- data


def test_french_numbers():
    from airconstruct.ui.tabs.results.data import fr, parse_fr

    assert fr(81.44) == "81,4"
    assert fr(-8.5, 1, sign=True) == "−8,5"
    assert fr(3.2, 1, sign=True) == "+3,2"
    assert fr(-0.01, 1) == "0,0"
    assert fr(math.nan) == "—"
    assert parse_fr("85,5 °C") == 85.5
    assert parse_fr("−3") == -3.0
    assert parse_fr("—") is None


def test_limits_and_rows(case):
    from airconstruct.ui.tabs.results.data import over_limit, part_limit, part_rows, rows_csv

    data = case.model_dump(mode="json")
    comps = {c["name"]: c for c in data["components"]}
    comps["cpu"]["max_temperature"] = 50.0  # set in the Modèle step
    comps["ram"]["max_temperature"] = 85.0
    assert part_limit(data, "cpu") == 50.0
    assert part_limit(data, "board") is None
    rows = part_rows(fake_summary(case, None), data)
    assert rows[0].name == "cpu" and rows[0].status == "ko"  # 55 > 50
    assert [r.name for r in over_limit(rows)] == ["cpu"]
    ram = next(r for r in rows if r.name == "ram")
    assert ram.status == "ok" and ram.margin == pytest.approx(42.0)
    csv = rows_csv(rows)
    assert csv.splitlines()[0].startswith("Pièce;Puissance (W)")
    assert "cpu;15,00;" in csv and csv.splitlines()[1].endswith(";KO")


def test_describe_changes(case):
    from airconstruct.ui.tabs.results.data import changes_line, describe_changes

    old = case.model_dump(mode="json")
    new = case.model_dump(mode="json")
    new["components"][1]["box"]["origin"]["x"] += 0.01  # cpu moved
    new["openings"][0]["flow_rate"] = 0.005
    new["components"].append({**new["components"][3], "name": "ram2"})
    new["ui"] = {"hidden": ["cpu"]}  # ignored
    texts = [c.text for c in describe_changes(old, new)]
    assert "cpu déplacé" in texts
    assert "inlet débit modifié" in texts
    assert "pièce ram2 ajoutée" in texts
    assert len(texts) == 3
    moved = next(c for c in describe_changes(old, new) if c.text == "cpu déplacé")
    assert moved.details == ["components.cpu.box.origin.x : 0,06 → 0,07"]
    assert changes_line(describe_changes(old, new), limit=2).endswith("et 1 autre")


def test_compare_rows_and_parameters(case):
    from airconstruct.ui.tabs.results.data import changed_parameters, compare_rows

    a = fake_summary(case, None)
    case_b = case.model_copy(deep=True)
    case_b.components[1].power = 6.5  # cpu: 15 W -> 6.5 W
    b = fake_summary(case_b, None)
    rows = compare_rows(a, b)
    cpu = next(r for r in rows if r.label == "cpu")
    assert cpu.cells() == ("55,0 °C", "46,5 °C", "−8,5 °C")
    assert rows[-1].label == "Perte de charge"
    data_a = case.model_dump(mode="json")
    data_b = case_b.model_dump(mode="json")
    data_b["openings"][0]["flow_rate"] = 20 / 3600
    params = {k: (va, vb) for k, va, vb in changed_parameters(data_a, data_b)}
    assert params["components.cpu.power"] == ("15", "6,5")
    assert params["openings.inlet · débit (m³/h)"] == ("11,5", "20,0")


# --------------------------------------------------------------------------- scene


def test_filter_pipeline_builds_every_kind(case):
    from airconstruct.ui.tabs.results.scene import (
        KINDS,
        PARTICLES,
        FieldData,
        Filter,
        build_scene,
        default_line,
        difference,
        particle_positions,
        sample_line,
        shifted,
    )

    d = FieldData(case, fake_regions(case))
    lo, hi = d.range("T")
    assert lo == pytest.approx(27.35, abs=0.5) and hi == pytest.approx(76.85, abs=0.01)
    filters = [Filter(k) for k in KINDS]
    items = build_scene(d, filters, "T", (lo, hi))
    names = [i.name for i in items]
    assert "enclosure" in names and "part:cpu" in names and PARTICLES in names
    for kind in ("slice", "iso", "vectors", "streamlines", "line"):
        assert any(n.startswith(kind) for n in names), kind
    cpu = next(i for i in items if i.name == "part:cpu")
    assert cpu.key == "component:cpu"
    # speed: solids shown in grey, vectors coloured by speed
    u_items = build_scene(d, filters, "U", d.range("U"))
    assert next(i for i in u_items if i.name == "part:cpu").scalars is None
    # line through the air from the inlet to the outlet: T rises along x
    _dist, vals = sample_line(d, *default_line(case), "T")
    ok = np.isfinite(vals)
    assert ok.sum() > 100 and vals[ok][-1] > vals[ok][0]
    # particles move along the streamlines
    lines = d.streamlines(36)
    p0, p1 = particle_positions(lines, 100, 0.0), particle_positions(lines, 100, 0.1)
    assert p0.shape == p1.shape and (p1[:, 0] > p0[:, 0]).mean() > 0.5
    # second run drawn to the right, same names + suffix
    moved = shifted(items[:3], 0.3, "#B")
    assert moved[0].name.endswith("#B") and moved[0].key is None
    assert moved[0].mesh.bounds[0] == pytest.approx(items[0].mesh.bounds[0] + 0.3)
    # difference of two runs on the same meshes
    d2 = FieldData(case, fake_regions(case, hot=360.0))
    diff = difference(d, d2, "T")
    assert diff.range("T")[1] == pytest.approx(10.0)
    other = FieldData(case, {"air": _grid((0, 0, 0), case.enclosure.as_tuple(), (5, 5, 5))})
    other.regions["air"].cell_data["T"] = np.full(other.regions["air"].n_cells, 300.0)
    assert difference(d, other, "T") is None


def test_filter_roundtrip():
    from airconstruct.ui.tabs.results.scene import Filter

    f = Filter("slice", params={"axis": "x", "position": 0.11})
    assert f.title == "Coupe verticale (x)"
    g = Filter.from_dict(f.to_dict())
    assert g.params == {"axis": "x", "position": 0.11} and g.visible
    assert Filter.from_dict({"kind": "nope"}) is None


def test_sankey_layout():
    from airconstruct.ui.tabs.results.charts import nice_ticks, sankey_layout

    sk = {"nodes": [{"id": "cpu", "kind": "part"}, {"id": "board", "kind": "part"},
                    {"id": "air", "kind": "air"}, {"id": "outlet", "kind": "opening"},
                    {"id": "walls", "kind": "walls"}],
          "links": [{"source": "cpu", "target": "board", "value": 2.0},
                    {"source": "cpu", "target": "air", "value": 6.0},
                    {"source": "board", "target": "air", "value": 2.0},
                    {"source": "air", "target": "outlet", "value": 7.5},
                    {"source": "board", "target": "walls", "value": 0.5}]}
    col, size = sankey_layout(sk)
    assert col["cpu"] == 0 and col["board"] == 1 and col["air"] == 2
    assert col["outlet"] == col["walls"] == 3
    assert size["cpu"] == 8.0 and size["air"] == 8.0
    assert nice_ticks(25.0, 82.0) == [40.0, 60.0, 80.0]
    assert nice_ticks(0.0, 1.0) == [0.0, 0.2, 0.4, 0.6000000000000001, 0.8, 1.0]


# --------------------------------------------------------------------------- tab


def _window(tmp_path, settings, case, **kw):
    kw.setdefault("run_fn", fake_run)
    kw.setdefault("summarize_fn", fake_summary)
    kw.setdefault("load_regions_fn", lambda d: fake_regions(case))
    kw.setdefault("open_report", False)
    return make_window(tmp_path, settings, case=case, **kw)


def test_empty_state_before_first_run(qapp, tmp_path, settings, case):
    win = _window(tmp_path, settings, case, estimate_fn=lambda c: 720.0)
    res = win.tabs["results"]
    assert not res.empty.isHidden() and res.kpi_box.isHidden()
    assert "12 min" in res.empty.text.text()
    assert "cpu" in res.empty.hottest.text()  # quick estimate
    assert not res.field_combo.isEnabled()
    assert res.scene() is None  # model geometry
    win.close()


def test_results_limits_alerts_and_stale_banner(qapp, tmp_path, settings, case):
    win = _window(tmp_path, settings, case)
    res = win.tabs["results"]
    run_and_wait(qapp, win)
    assert res.empty.isHidden() and not res.kpi_box.isHidden()
    assert res.kpis.hottest.text() == "cpu · 55,0 °C"
    assert res.kpis.values["Vitesse max"].text() == "1,50 m/s"
    assert res.results.table.rowCount() == 4
    assert res.results.table.item(0, res.results.STATE_COL).text() == "—"
    # a limit set on the part (Modèle step): KO, alert on the tab
    win.project.edit("Limite", lambda d: d["components"][1].update(max_temperature=50.0))
    assert res.results.table.item(0, res.results.LIMIT_COL).text() == "50"
    assert res.results.table.item(0, res.results.STATE_COL).text() == "✗"
    assert [a.message for a in win.alerts.for_tab("results")] == [
        "cpu dépasse sa limite (55,0 / 50 °C)"]
    assert win.tab_badge("results") == "1"
    assert "cpu dépasse" in res.kpis.limits.text()
    # double-click a part: opens it in the Modèle step
    res.results.table.cellDoubleClicked.emit(0, 0)
    assert win.current_tab is win.tabs["model"] and win.selection.keys == ["component:cpu"]
    win.project.undo_stack.undo()
    assert win.alerts.for_tab("results") == []
    # model changed: banner says what changed
    win.project.edit("Déplacer", lambda d: d["components"][1]["box"]["origin"].update(x=0.07))
    assert win.project.stale and not res.banner.isHidden()
    assert "Cpu déplacé" in res.banner.changes_label.text()
    res.banner.changes_button.click()
    assert not res.banner.details.isHidden()
    assert "origin.x" in res.banner.details.text()
    assert res.banner.rerun_button.text().startswith("Relancer · ≈")
    win.close()


def test_scene_filters_line_and_particles(qapp, tmp_path, settings, case):
    win = _window(tmp_path, settings, case)
    res = win.tabs["results"]
    run_and_wait(qapp, win)
    win.goto_tab("results")
    names = [i.name for i in win.viewport.items]
    assert "part:cpu" in names and any(n.startswith("streamlines") for n in names)
    panel = res.filters_panel
    assert [f.kind for f in panel.filters] == ["parts", "slice", "streamlines"]
    panel.add("line")
    assert not res.line_box.isHidden()
    assert np.isfinite(res.line_chart.values).sum() > 50
    panel.add("particles")
    qapp.processEvents()
    res.set_playing(True)
    assert res.playing
    win.refresh_scene()
    item = next(i for i in win.viewport.items if i.name == "particles")
    before = np.array(item.mesh.points)
    res._tick()
    assert not np.allclose(before, item.mesh.points)
    # hiding a filter removes it from the view; the pipeline is remembered
    panel.list.item(0).setCheckState(panel.list.item(0).checkState().__class__.Unchecked)
    win.refresh_scene()
    assert "part:cpu" not in [i.name for i in win.viewport.items]
    assert '"kind": "particles"' in settings.value("results/filters")
    # speed instead of temperature
    res.field_combo.setCurrentIndex(1)
    assert "m/s" in res.scale_label.text()
    res.auto_scale.setChecked(False)
    res.scale_min.setValue(0.0)
    res.scale_max.setValue(2.0)
    assert res.clim() == (0.0, 2.0)
    win.close()


def test_compare_two_runs(qapp, tmp_path, settings, case):
    win = _window(tmp_path, settings, case)
    res = win.tabs["results"]
    run_and_wait(qapp, win)
    win.project.edit("Puissance", lambda d: d["components"][1].update(power=6.5))
    run_and_wait(qapp, win)
    assert [e.number for e in res.history] == [1, 2]
    res._fill_compare_menu(res.compare_menu)
    acts = [a for a in res.compare_menu.actions() if a.isEnabled() and not a.isSeparator()]
    assert acts and "Calcul 1" in acts[0].text()
    acts[0].trigger()
    assert res.compare == (0, 1)
    assert not res.compare_view.isHidden() and res.kpi_box.isHidden()
    t = res.compare_view.table
    assert [t.item(0, c).text() for c in range(4)] == ["cpu", "55,0 °C", "46,5 °C", "−8,5 °C"]
    p = res.compare_view.params
    assert p.item(0, 0).text() == "components.cpu.power"
    win.goto_tab("results")
    names = [i.name for i in win.viewport.items]
    assert "part:cpu" in names and "part:cpu#B" in names
    res.compare_view.diff_button.click()
    win.refresh_scene()
    assert not any(n.endswith("#B") for n in (i.name for i in win.viewport.items))
    res.stop_compare()
    assert res.compare is None and not res.kpi_box.isHidden()
    win.close()


def test_exports(qapp, tmp_path, settings, case):
    pytest.importorskip("matplotlib")
    win = _window(tmp_path, settings, case)
    res = win.tabs["results"]
    run_and_wait(qapp, win)
    csv = res.export_csv(tmp_path / "pieces.csv")
    assert csv.read_text(encoding="utf-8-sig").startswith("Pièce;")
    vtm = res.export_vtk_fields(tmp_path / "champs")
    assert vtm.suffix == ".vtm" and vtm.exists()
    assert set(pv.read(vtm).keys()) == {"air", "cpu"}
    vtp = res.export_vtk_streamlines(tmp_path / "lignes")
    assert vtp.exists() and pv.read(vtp).n_points > 0
    pdf = res.export_pdf(tmp_path / "rapport")
    assert pdf.suffix == ".pdf" and pdf.read_bytes().startswith(b"%PDF")
    assert res.last_report == pdf
    win.close()


def test_compare_with_saved_runs(qapp, tmp_path, settings, case):
    from airconstruct.runs import RunStore

    win = _window(tmp_path, settings, case)
    res = win.tabs["results"]
    run_and_wait(qapp, win)
    store = RunStore(tmp_path / "runs")
    saved = store.list(name=case.name)
    assert len(saved) == 1 and saved[0].status == "converged" and saved[0].label == "interface"
    assert saved[0].summary["components"][0]["name"] == "board"
    # a later session: the run is listed under "Calculs enregistrés"
    win.close()
    win = _window(tmp_path, settings, case)
    res = win.tabs["results"]
    win.project.edit("Puissance", lambda d: d["components"][1].update(power=6.5))
    run_and_wait(qapp, win)
    res._fill_compare_menu(res.compare_menu)
    texts = [a.text() for a in res.compare_menu.actions()]
    assert "Calculs enregistrés" in texts
    act = next(a for a in res.compare_menu.actions() if a.text().startswith("Calcul du "))
    act.trigger()
    a, b = (res.history[i] for i in res.compare)
    assert a.name.startswith("Calcul du ") and b is res.current
    t = res.compare_view.table
    assert [t.item(0, c).text() for c in range(4)] == ["cpu", "55,0 °C", "46,5 °C", "−8,5 °C"]
    assert res.compare_view.params.item(0, 0).text() == "components.cpu.power"
    win.close()


def test_saved_run_fields_from_its_own_folder(tmp_path, case):
    from airconstruct.runs import RunStore
    from airconstruct.ui.tabs.results import history

    store = RunStore(tmp_path)
    rec = store.add(case, label="ventilateur 80 %")
    Path(rec.case_dir).mkdir(parents=True)
    store.update(rec.id, status="finished", summary=fake_summary(case, None).to_dict())
    rec = store.get(rec.id)
    record = history.as_record(rec, lambda d: fake_regions(case))
    assert set(record.outcome.regions) == {"air", "cpu"}
    assert record.outcome.summary.hottest().name == "cpu"
    assert history.saved_runs(store, case.name, set()) == [rec]
    assert history.saved_runs(store, case.name, {rec.id}) == []
