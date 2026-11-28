"""CAD import: readers, storage, fitting, voxelization, mesh of imported shapes."""

from __future__ import annotations

import numpy as np
import pytest
from cad_shapes import (
    box_triangles,
    glb,
    sphere_triangles,
    write_ascii_stl,
    write_binary_stl,
    write_step_box,
)

from airconstruct import cad
from airconstruct.case import Case, Probe, Refinement, Vec3
from airconstruct.mesh import plan_mesh, shape_cells, write_mesh_dicts


def _case(shape_tris=None, box=((0.02, 0.02, 0.0), (0.04, 0.04, 0.04))) -> Case:
    lo, hi = box
    comp = {"name": "part", "material": "aluminium", "power": 2.0,
            "box": {"origin": dict(zip("xyz", lo)),
                    "size": dict(zip("xyz", np.subtract(hi, lo)))}}
    if shape_tris is not None:
        comp["shape"] = {"data": cad.encode(shape_tris), "source": "s.stl"}
    return Case.model_validate({
        "name": "cad", "enclosure": {"x": 0.08, "y": 0.06, "z": 0.05},
        "components": [comp],
        "openings": [{"name": "inlet", "kind": "inlet", "face": "xmin", "center": [0.03, 0.025],
                      "size": [0.02, 0.02], "velocity": 1.0},
                     {"name": "outlet", "kind": "outlet", "face": "xmax",
                      "center": [0.03, 0.025], "size": [0.02, 0.02]}],
        "precision": {"level": 3}})


# ----------------------------------------------------------------------- readers
def test_ascii_stl_bodies_and_units(tmp_path):
    f = write_ascii_stl(tmp_path / "two.stl", {
        "socle": box_triangles((0, 0, 0), (10, 20, 2)),
        "bloc": box_triangles((2, 2, 2), (8, 8, 12))})
    bodies = cad.read(f)  # mm by default
    assert [b.name for b in bodies] == ["socle", "bloc"]
    assert np.allclose(bodies[0].size, [0.010, 0.020, 0.002])
    assert np.allclose(cad.read(f, "in")[0].size, [0.254, 0.508, 0.0508])


def test_binary_stl(tmp_path):
    # a header starting with "solid" must not fool the reader
    f = write_binary_stl(tmp_path / "b.stl", box_triangles((0, 0, 0), (5, 5, 5)),
                         header=b"solid made by some exporter")
    (body,) = cad.read(f)
    assert body.name == "b" and len(body.triangles) == 12
    assert np.allclose(body.size, 0.005)


def test_unknown_format(tmp_path):
    with pytest.raises(cad.CadError):
        cad.read(tmp_path / "x.obj")


def test_glb_reader_applies_node_translation():
    raw = glb({"a": box_triangles((0, 0, 0), (1, 1, 1))}, {"a": (5.0, 0.0, 0.0)})
    (b,) = cad.read_glb(raw)
    lo, hi = b.bounds
    assert b.name == "a" and np.allclose(lo, [5, 0, 0]) and np.allclose(hi, [6, 1, 1])


def test_step_box(tmp_path):
    pytest.importorskip("cascadio")
    f = write_step_box(tmp_path / "boite.step", lo_mm=(5, 0, 0), size_mm=(10, 20, 30))
    (b,) = cad.read(f)
    lo, hi = b.bounds
    # file in mm, read in metres, z still up
    assert np.allclose(hi - lo, [0.010, 0.020, 0.030], atol=1e-6)
    assert np.allclose(lo, [0.005, 0.0, 0.0], atol=1e-6)
    assert cad.open_edges(b.triangles) == 0


# ----------------------------------------------------------------------- geometry
def test_encode_decode_round_trip():
    s = sphere_triangles((0.1, 0.2, 0.3), 0.05, 12)
    back = cad.decode(cad.encode(s))
    assert back.shape == s.shape and np.allclose(back, s, atol=1e-7)
    with pytest.raises(ValueError):
        cad.decode("not base64 !")


def test_checks_closed_volume_split():
    s = sphere_triangles((0, 0, 0), 1.0, 32)
    assert cad.open_edges(s) == 0
    assert cad.open_edges(s[:-5]) > 0
    assert cad.volume(s) == pytest.approx(4 / 3 * np.pi, rel=0.01)
    two = cad.Body("x", np.concatenate([box_triangles((3, 3, 3), (4, 4, 4)), s]))
    parts = cad.split_bodies(two)
    assert [len(p.triangles) for p in parts] == [len(s), 12]


def test_fit_maps_bounding_box():
    t = cad.fit(box_triangles((0, 0, 0), (1, 2, 4)), (1, 1, 1), (2, 2, 2))
    pts = t.reshape(-1, 3)
    assert np.allclose(pts.min(axis=0), 1) and np.allclose(pts.max(axis=0), 2)


def test_voxelize_sphere_and_box():
    xc = (np.arange(40) + 0.5) / 40
    v = cad.voxelize(sphere_triangles((0.5, 0.5, 0.5), 0.4, 32), xc, xc, xc)
    assert v.mean() == pytest.approx(4 / 3 * np.pi * 0.4**3, rel=0.03)
    assert v[20, 20, 20] and not v[0, 0, 0]
    v = cad.voxelize(box_triangles((0.25, 0.25, 0.25), (0.75, 0.75, 0.75)), xc, xc, xc)
    assert v.sum() == 20**3


# ----------------------------------------------------------------------- case, mesh
def test_case_keeps_shape_and_saves(tmp_path):
    case = _case(box_triangles((0, 0, 0), (1, 1, 1)))
    assert case.components[0].shape is not None
    case.save(tmp_path / "c.yaml")
    again = Case.load(tmp_path / "c.yaml")
    assert again.components[0].shape.data == case.components[0].shape.data
    with pytest.raises(ValueError):
        Case.model_validate({**case.model_dump(mode="json"),
                             "components": [{**case.components[0].model_dump(mode="json"),
                                             "shape": {"data": "xx"}}]})


def test_shape_cells_follow_the_shape():
    # a sphere in the part's box: fewer cells than the box, about pi/6 of them
    case = _case(sphere_triangles((0, 0, 0), 1.0, 32))
    plan = plan_mesh(case)
    labels = shape_cells(plan, case.components[0])
    box_case = _case()
    full = shape_cells(plan_mesh(box_case), box_case.components[0].model_copy(
        update={"shape": case.components[0].shape.model_copy(
            update={"data": cad.encode(box_triangles())})}))
    assert 0.45 < len(labels) / len(full) < 0.6
    # labels are blockMesh cells (x fastest) whose centre is in the sphere
    nx, ny, _ = plan.divisions
    cs = [g.centres() for g in plan.axes]
    i, j, k = labels % nx, (labels // nx) % ny, labels // (nx * ny)
    p = np.stack([cs[0][i], cs[1][j], cs[2][k]], axis=1)
    # the unit sphere fitted in the 20 x 20 x 40 mm box is an ellipsoid
    r = (p - [0.03, 0.03, 0.02]) / [0.01, 0.01, 0.02]
    assert (np.linalg.norm(r, axis=1) < 1.01).all()


def test_axis_grid_nodes_match_spacings():
    case = _case()
    case.refinements = [Refinement(target="part", factor=3)]  # graded segments
    plan = plan_mesh(case)
    for g in plan.axes:
        x = g.nodes()
        assert len(x) == g.n + 1 and x[0] == 0 and np.all(np.diff(x) > 0)
        for b in g.breakpoints:
            assert np.min(np.abs(x - b)) < 1e-12


def test_mesh_dicts_use_labels_for_shapes(tmp_path):
    case = _case(sphere_triangles((0, 0, 0), 1.0, 16))
    write_mesh_dicts(case, tmp_path)
    zones = (tmp_path / "system" / "topoSetDict.zones").read_text()
    assert "labelToCell" in zones and "boxToCell" in zones  # part labels, air box


def test_too_thin_shape_falls_back_to_box(tmp_path):
    # a thin plate at the bottom and a tiny cube at the top: once fitted in the box
    # neither holds a cell centre
    plate = box_triangles((0, 0, 0.0), (1, 1, 0.001))
    marker = box_triangles((0, 0, 0.999), (0.001, 0.001, 1.0))
    case = _case(np.concatenate([plate, marker]))
    plan = write_mesh_dicts(case, tmp_path)
    assert any("thinner than the mesh" in w for w in plan.warnings)
    assert "labelToCell" not in (tmp_path / "system" / "topoSetDict.zones").read_text()


def test_probe_region_uses_shape():
    case = _case(sphere_triangles((0, 0, 0), 1.0, 16))
    corner = case.model_copy(update={"probes": []})
    inside = Probe(name="a", point=Vec3(x=0.03, y=0.03, z=0.02))
    out = Probe(name="b", point=Vec3(x=0.0205, y=0.0205, z=0.001))
    assert corner.probe_region(inside) == "part"
    assert corner.probe_region(out) == "air"
