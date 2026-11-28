"""CAD import in the Modèle tab: pure operations, the dialog, the window flows."""

from __future__ import annotations

import copy

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from cad_shapes import box_triangles, sphere_triangles, write_ascii_stl
from test_tab_model import data as model_data
from ui_support import make_window

from airconstruct import cad
from airconstruct.case import Case
from airconstruct.ui.tabs.model import cadparts
from airconstruct.ui.tabs.model import geometry as geo
from airconstruct.ui.tabs.model.cad_dialog import CadImport, CadImportDialog


def bodies():
    """A heat sink base with a chip under it (mm), as an assembly would have them."""
    return [cad.Body("base", box_triangles((0, 0, 0.002), (0.04, 0.03, 0.006))),
            cad.Body("puce", sphere_triangles((0.02, 0.015, 0.001), 0.001, 8))]


# ---------------------------------------------------------------------- operations
def test_new_project_around_parts():
    d = cadparts.new_project(bodies(), "essai", "assemblage.step", "copper")
    E = geo.enclosure(d)
    lo, hi = geo.bbox(d, ["base", "puce"])
    assert all(lo[i] > 0 and hi[i] < E[i] for i in range(3))  # room all around
    assert lo[0] == pytest.approx(E[0] - hi[0])  # centred
    assert geo.read_meta(d)["groups"] == {"assemblage": ["base", "puce"]}
    assert geo.comp(d, "base")["material"] == "copper"
    assert geo.comp(d, "base")["shape"]["source"] == "assemblage.step"
    # relative positions kept: the chip is still centred under the base
    b, p = geo.lo_hi(geo.comp(d, "base")), geo.lo_hi(geo.comp(d, "puce"))
    assert (p[0][0] + p[1][0]) / 2 == pytest.approx((b[0][0] + b[1][0]) / 2)
    assert p[1][2] == pytest.approx(b[0][2])
    # no openings yet, but the parts themselves are valid
    with pytest.raises(ValueError, match="inlet"):
        Case.model_validate(d)


def test_add_to_project_grows_enclosure_and_drop_shape():
    d = model_data()
    big = [cad.Body("chassis", box_triangles((0, 0, 0), (0.3, 0.05, 0.02)))]
    names = cadparts.add_bodies(d, big, "chassis.stl")
    assert names == ["chassis"]
    assert geo.enclosure(d)[0] > 0.3 and geo.enclosure(d)[1] == pytest.approx(0.12)
    assert not geo.outside(d)
    assert geo.read_meta(d)["kinds"]["chassis"] == cadparts.KIND
    assert "chassis.stl" in cadparts.shape_info(geo.comp(d, "chassis"))
    case = Case.model_validate(d)
    assert case.components[-1].shape is not None
    # a CAD part's box overlapping a plain part is not reported as an overlap
    assert not [o for o in geo.overlaps(d) if "chassis" in o[:2]]
    assert cadparts.drop_shape(d, ["chassis", "cpu"]) == ["chassis"]
    assert "shape" not in geo.comp(d, "chassis")
    assert cadparts.shape_info(geo.comp(d, "chassis")) == ""


def test_names_do_not_clash():
    d = model_data()
    names = cadparts.add_bodies(d, [cad.Body("cpu", box_triangles((0, 0, 0), (0.01,) * 3))])
    assert names == ["cpu_2"]


# ---------------------------------------------------------------------- dialog
def test_dialog_units_split_and_open_surface(qapp, tmp_path):
    s = sphere_triangles((40, 40, 10), 8, 12)
    f = write_ascii_stl(tmp_path / "ensemble.stl", {
        "tout": np.concatenate([box_triangles((0, 0, 0), (80, 60, 2)), s]),
        "ouvert": box_triangles((0, 0, 30), (5, 5, 35))[:-2]})
    dlg = CadImportDialog(f)
    assert dlg.error is None and dlg.ok_button.isEnabled()
    assert [b.name for b in dlg.bodies] == ["tout_1", "tout_2", "ouvert"]
    assert np.allclose(dlg.bodies[1].size, [0.08, 0.06, 0.002])  # mm by default (sphere first)
    assert "ouvert" in dlg.warning.text()
    dlg.split.setChecked(False)
    assert len(dlg.bodies) == 2
    dlg.unit.setCurrentIndex(dlg.unit.findData("cm"))
    assert np.allclose(dlg.bodies[0].size, [0.8, 0.6, 0.18])
    dlg.material.setCurrentIndex(dlg.material.findData("copper"))
    dlg.accept_import()
    assert dlg.result_import.material == "copper" and dlg.result_import.source == "ensemble.stl"
    dlg.close()


def test_dialog_bad_file(qapp, tmp_path):
    f = tmp_path / "vide.stl"
    f.write_text("rien")
    dlg = CadImportDialog(f)
    assert dlg.error and not dlg.ok_button.isEnabled()
    assert "Lecture impossible" in dlg.warning.text()
    dlg.close()


# ---------------------------------------------------------------------- window
@pytest.fixture
def win(qapp, tmp_path, settings):
    w = make_window(tmp_path, settings)
    yield w
    w.close()


def _choice(tmp_path, bs=None):
    return CadImport(tmp_path / "assemblage.step", bs or bodies(), "aluminium")


def test_tab_import_draws_shapes_and_undoes(win, tmp_path):
    tab = win.tabs["model"]
    tab.ask_cad_import = lambda parent, path: _choice(tmp_path)
    names = tab.import_cad()
    assert names == ["base", "puce"]
    assert win.selection.keys == ["component:base", "component:puce"]
    case = win.project.case
    assert case is not None and case.components[-1].shape is not None
    puce = next(i for i in tab.scene() if i.name == "puce")
    assert puce.mesh.n_cells > 12  # the sphere, not its box
    assert "show_edges" not in puce.kwargs
    from airconstruct.ui.view import geometry_items

    assert next(i for i in geometry_items(case) if i.name == "puce").mesh.n_cells > 12
    win.selection.set(["component:puce"])
    tab.inspector.refresh()
    assert tab.inspector.shape_note.isVisibleTo(tab.inspector)
    assert "assemblage.step" in tab.inspector.shape_note.text()
    tab.drop_shape()
    assert win.project.case.components[-1].shape is None
    win.undo_action.trigger()
    win.undo_action.trigger()
    assert "puce" not in win.project.case.solid_regions
    tab.ask_cad_import = lambda parent, path: None
    assert tab.import_cad() == []


def test_open_cad_file_makes_new_project(win, tmp_path):
    f = write_ascii_stl(tmp_path / "boitier.stl", {"bloc": box_triangles((0, 0, 0), (20, 10, 5))})
    seen = {}

    def ask(parent, path):
        seen["path"] = path
        return CadImport(path, cad.read(path), "steel")

    win.ask_cad_import = ask
    assert win.load_case(f)
    assert seen["path"] == f
    d = win.project.data
    assert d["name"] == "boitier" and win.project.path is None
    assert geo.comp(d, "bloc")["material"] == "steel"
    assert win.current_tab is win.tabs["model"]


def test_new_project_dialog_cad_template(win, tmp_path):
    from airconstruct.ui.tabs.model.library import templates
    from airconstruct.ui.tabs.model.new_project import NewProjectDialog

    dlg = NewProjectDialog(templates(win.project.data), [], win)
    dlg.ask_cad_import = lambda parent, path: _choice(tmp_path)
    dlg.choose("cad")
    dlg.create()
    assert dlg.data["name"] == "assemblage"
    assert [c["name"] for c in dlg.data["components"]] == ["base", "puce"]
    dlg.close()
    dlg = NewProjectDialog(templates(win.project.data), [], win)
    dlg.ask_cad_import = lambda parent, path: None
    dlg.choose("cad")
    dlg.create()
    assert dlg.data is None  # cancelled: the dialog stays open
    dlg.close()


def test_shape_survives_save_and_reopen(win, tmp_path):
    tab = win.tabs["model"]
    tab.ask_cad_import = lambda parent, path: _choice(tmp_path)
    tab.import_cad()
    before = copy.deepcopy(win.project.data)
    path = tmp_path / "projet.yaml"
    assert win.write_case(path)
    assert win.load_case(path)
    assert win.project.data["components"] == before["components"]
