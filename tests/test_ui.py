"""UI tests: offscreen Qt, 3D view disabled, runner/results mocked."""

from __future__ import annotations

import gc
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["AIRCONSTRUCT_NO_3D"] = "1"

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from airconstruct.case import Case
from airconstruct.results import ComponentResult, Summary
from airconstruct.runner import RunResult
from airconstruct.ui.app import MainWindow
from airconstruct.ui.form import CaseForm, FormError, format_duration

SHIPPED_EXAMPLE = Path(__file__).parent.parent / "examples" / "electronics_box.yaml"
# frozen geometry (same as test_results): the expectations below depend on it
EXAMPLE = Path(__file__).parent / "data" / "results_box.yaml"


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def qapp(_app):
    """Destroy every widget a test created while the QApplication is still alive:
    widgets left to Python's garbage collector at interpreter exit can segfault."""
    yield _app
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _app.processEvents()
    gc.collect()


def wait_until(qapp, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


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


def fake_run(case, work_dir, backend=None, on_log=None, on_residual=None, should_stop=None):
    on_log("fake solver start")
    for i in range(1, 6):
        on_residual(i, {"Ux": 10.0 ** -i, "h": 0.5 * 10.0 ** -i})
    case_dir = Path(work_dir) / case.name
    case_dir.mkdir(parents=True, exist_ok=True)
    return RunResult(case_dir=case_dir, success=True, converged=True, iterations=5,
                     wall_time_s=1.0)


def fake_summary(case, case_dir):
    return Summary(
        components=[ComponentResult(c.name, c.power, 40.0 + c.power, 30.0) for c in case.components],
        air_t_max_c=35.0, air_v_max=1.5, pressure_drop_pa=2.0, outlet_t_mean_c=28.0,
    )


def test_window_run_with_mocks(qapp, tmp_path):
    win = MainWindow(case=Case.load(EXAMPLE), work_dir=tmp_path, run_fn=fake_run,
                     summarize_fn=fake_summary, load_regions_fn=lambda d: {},
                     estimate_fn=lambda c: 90.0)
    assert win.run_button.isEnabled()
    assert "2 min" in win.form.estimate_label.text()
    win.start_run()
    assert wait_until(qapp, lambda: win.thread is None and win.outcome is not None)
    assert win.outcome.success
    assert "Converged" in win.progress.format()
    assert win.results.table.rowCount() == 4
    assert win.results.table.item(0, 0).text() == "cpu"  # hottest first
    assert "fake solver start" in win.log.toPlainText()
    assert win.run_button.isEnabled() and win.form.isEnabled()
    win.close()


def test_window_without_openfoam(qapp, tmp_path, monkeypatch):
    """Default runner path: a missing OpenFOAM is reported, not crashed on."""
    from airconstruct import runner

    class NoFoam(runner.Backend):
        def available(self):
            return False, "OpenFOAM not found (test)"

    monkeypatch.setattr(runner, "detect_backend", lambda: NoFoam())
    win = MainWindow(case=Case.load(EXAMPLE), work_dir=tmp_path)
    win.start_run()
    assert wait_until(qapp, lambda: win.thread is None and win.outcome is not None)
    assert not win.outcome.success
    assert win.outcome.errors == ["OpenFOAM is not available: OpenFOAM not found (test)"]
    assert "OpenFOAM not found" in win.results.headline.text()
    win.close()


def test_window_default_runner_gets_backend(qapp, tmp_path, monkeypatch):
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
    win = MainWindow(case=Case.load(EXAMPLE), work_dir=tmp_path, summarize_fn=fake_summary,
                     load_regions_fn=lambda d: {})
    win.start_run()
    assert wait_until(qapp, lambda: win.thread is None and win.outcome is not None)
    assert isinstance(seen["backend"], FakeFoam)
    assert win.outcome.success
    assert "Using OpenFOAM v2406 (test)" in win.log.toPlainText()
    win.close()


def test_quick_estimate_preview(qapp, tmp_path):
    win = MainWindow(case=Case.load(EXAMPLE), work_dir=tmp_path, estimate_fn=lambda c: 1.0)
    assert "Quick estimate" in win.results.headline.text()
    assert win.results.table.rowCount() == 4
    assert win.results.table.item(0, 0).text() == "cpu"  # 15 W: hottest
    t_cpu = float(win.results.table.item(0, 2).text())
    assert 25.0 < t_cpu < 300.0
    win.close()


def test_phase_and_progress(qapp, tmp_path):
    win = MainWindow(case=Case.load(EXAMPLE), work_dir=tmp_path, estimate_fn=lambda c: 1.0)
    win.last_run_case = win.current_case
    win.progress.setRange(0, 100)
    win._on_log("$ blockMesh")
    assert "Meshing (blockMesh)" in win.status.currentMessage()
    win._on_log("--- phase flow: iterations 0..400")
    assert win.status.currentMessage() == "Solving air flow"
    win._on_residual(150, {"Ux": 1e-3})
    assert win.progress.maximum() == win.current_case.solver.iterations
    assert win.progress.value() == 150
    win.close()


def test_window_invalid_case_disables_run(qapp, tmp_path):
    win = MainWindow(case=Case.load(EXAMPLE), work_dir=tmp_path, estimate_fn=lambda c: 1.0)
    win.form.openings.removeRow(1)  # no outlet left
    win.refresh()
    assert not win.run_button.isEnabled()
    assert "outlet" in win.status.currentMessage()
    win.close()


def test_window_save_load(qapp, tmp_path):
    win = MainWindow(case=Case.load(EXAMPLE), work_dir=tmp_path, estimate_fn=lambda c: 1.0)
    p = tmp_path / "saved.yaml"
    assert win.write_case(p)
    win.load_case(p)
    assert _close(win.form.to_case(), Case.load(EXAMPLE))
    win.close()


def test_window_with_real_results_module(qapp, tmp_path):
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

    win = MainWindow(case=case, work_dir=tmp_path / "runs", run_fn=run,
                     estimate_fn=lambda c: 1.0, open_report=False)
    win.start_run()
    assert wait_until(qapp, lambda: win.thread is None and win.outcome is not None, 60)
    assert win.outcome.success, win.outcome.errors
    assert win.results.table.item(0, 0).text() == "cpu"
    assert float(win.results.table.item(0, 2).text()) == pytest.approx(76.9, abs=0.5)
    assert set(win.outcome.regions) >= {"air", "cpu"}
    assert win.field_combo.isEnabled()
    win.save_report()
    assert win.last_report is not None and win.last_report.exists()
    win.close()


@pytest.mark.openfoam
def test_window_real_openfoam_run(qapp, tmp_path):
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
    win = MainWindow(case=case, work_dir=tmp_path / "runs", open_report=False)
    win.start_run()
    assert wait_until(qapp, lambda: win.thread is None and win.outcome is not None, 1200)
    log = win.log.toPlainText()
    assert win.outcome.success, "\n".join(win.outcome.errors) + "\n" + log[-3000:]
    assert "--- phase flow" in log
    assert win.outcome.summary is not None
    assert "air" in win.outcome.regions
    assert win.results.table.rowCount() == len(case.components)
    win.save_report()
    assert win.last_report is not None and win.last_report.exists()
    win.close()
