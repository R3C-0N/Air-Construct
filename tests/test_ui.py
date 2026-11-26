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

EXAMPLE = Path(__file__).parent.parent / "examples" / "electronics_box.yaml"


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


def test_window_runner_not_implemented(qapp, tmp_path):
    # default estimate and runner are still stubs on main: the UI must not crash
    win = MainWindow(case=Case.load(EXAMPLE), work_dir=tmp_path)
    win.start_run()
    assert wait_until(qapp, lambda: win.thread is None and win.outcome is not None)
    assert not win.outcome.success
    assert win.outcome.errors
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
