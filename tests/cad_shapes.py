"""Closed triangle surfaces and CAD files for the tests (no CAD library needed)."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np


def box_triangles(lo=(0, 0, 0), hi=(1, 1, 1)) -> np.ndarray:
    """12 outward triangles of an axis-aligned box."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return v[np.array(f)]


def sphere_triangles(center=(0, 0, 0), radius=1.0, n=24) -> np.ndarray:
    """UV sphere, closed (poles shared)."""
    c = np.asarray(center, dtype=float)
    tris = []
    th = np.linspace(0, np.pi, n + 1)
    ph = np.linspace(0, 2 * np.pi, 2 * n + 1)

    def p(i, j):
        return c + radius * np.array([np.sin(th[i]) * np.cos(ph[j % (2 * n)]),
                                      np.sin(th[i]) * np.sin(ph[j % (2 * n)]), np.cos(th[i])])

    for i in range(n):
        for j in range(2 * n):
            a, b, cc, d = p(i, j), p(i + 1, j), p(i + 1, j + 1), p(i, j + 1)
            if i > 0:
                tris.append([a, b, d])
            if i < n - 1:
                tris.append([b, cc, d])
    return np.array(tris)


def write_ascii_stl(path: Path, solids: dict[str, np.ndarray]) -> Path:
    lines = []
    for name, tris in solids.items():
        lines.append(f"solid {name}")
        for t in tris:
            lines += ["  facet normal 0 0 0", "    outer loop"]
            lines += [f"      vertex {x:.9e} {y:.9e} {z:.9e}" for x, y, z in t]
            lines += ["    endloop", "  endfacet"]
        lines.append(f"endsolid {name}")
    path.write_text("\n".join(lines) + "\n")
    return path


def write_binary_stl(path: Path, tris: np.ndarray, header: bytes = b"binary") -> Path:
    tris = np.asarray(tris, dtype="<f4")
    out = bytearray(header.ljust(80, b"\0")[:80])
    out += struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0) + t.tobytes() + b"\0\0"
    path.write_bytes(bytes(out))
    return path


def glb(meshes: dict[str, np.ndarray], translations: dict[str, tuple] | None = None) -> bytes:
    """A glTF binary with one node per mesh (indexed, uint32), optional translations."""
    translations = translations or {}
    binary = bytearray()
    js = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": []}], "nodes": [],
          "meshes": [], "accessors": [], "bufferViews": [], "buffers": []}
    for name, tris in meshes.items():
        pts = np.asarray(tris, dtype="<f4").reshape(-1, 3)
        idx = np.arange(len(pts), dtype="<u4")
        for arr, kind, ctype in ((pts, "VEC3", 5126), (idx, "SCALAR", 5125)):
            while len(binary) % 4:
                binary += b"\0"
            js["bufferViews"].append({"buffer": 0, "byteOffset": len(binary),
                                      "byteLength": arr.nbytes})
            binary += arr.tobytes()
            js["accessors"].append({"bufferView": len(js["bufferViews"]) - 1,
                                    "componentType": ctype, "count": len(arr), "type": kind})
        n = len(js["accessors"])
        js["meshes"].append({"name": name, "primitives": [
            {"attributes": {"POSITION": n - 2}, "indices": n - 1, "mode": 4}]})
        node = {"name": name, "mesh": len(js["meshes"]) - 1}
        if name in translations:
            node["translation"] = list(translations[name])
        js["nodes"].append(node)
        js["scenes"][0]["nodes"].append(len(js["nodes"]) - 1)
    js["buffers"].append({"byteLength": len(binary)})
    jb = json.dumps(js).encode()
    jb += b" " * (-len(jb) % 4)
    binary += b"\0" * (-len(binary) % 4)
    body = (struct.pack("<II", len(jb), 0x4E4F534A) + jb
            + struct.pack("<II", len(binary), 0x004E4942) + bytes(binary))
    return b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body


def write_step_box(path: Path, lo_mm=(0, 0, 0), size_mm=(10, 20, 30), unit: str = "MILLI"
                   ) -> Path:
    """A STEP AP214 file holding one box solid (planar B-rep), lengths in mm (or in
    metres with ``unit=None``)."""
    ents: list[str] = []

    def add(text: str) -> str:
        ents.append(text)
        return f"#{len(ents)}"

    def pt(p) -> str:
        return add("CARTESIAN_POINT('',(" + ",".join(f"{float(v):.6f}" for v in p) + "))")

    def dirn(d) -> str:
        return add("DIRECTION('',(" + ",".join(f"{float(v):.1f}" for v in d) + "))")

    lo = np.asarray(lo_mm, dtype=float)
    sz = np.asarray(size_mm, dtype=float)
    corner = {b: lo + sz * np.array(b) for b in np.ndindex(2, 2, 2)}
    vtx = {b: add(f"VERTEX_POINT('',{pt(p)})") for b, p in corner.items()}
    edges = {}
    for b, p in corner.items():
        for ax in range(3):
            if b[ax] == 0:
                e = list(b)
                e[ax] = 1
                e = tuple(e)
                d = [0.0, 0.0, 0.0]
                d[ax] = 1.0
                vec = add(f"VECTOR('',{dirn(d)},{sz[ax]:.6f})")
                line = add(f"LINE('',{pt(p)},{vec})")
                edges[(b, e)] = add(f"EDGE_CURVE('',{vtx[b]},{vtx[e]},{line},.T.)")
    faces = []
    for ax in range(3):
        u, v = (ax + 1) % 3, (ax + 2) % 3
        for side in (0, 1):
            sq = [(0, 0), (1, 0), (1, 1), (0, 1)]
            if side == 0:
                sq = sq[::-1]
            cs = []
            for a, b in sq:
                c = [0, 0, 0]
                c[ax], c[u], c[v] = side, a, b
                cs.append(tuple(c))
            oes = []
            for k in range(4):
                p, q = cs[k], cs[(k + 1) % 4]
                if (p, q) in edges:
                    oes.append(add(f"ORIENTED_EDGE('',*,*,{edges[(p, q)]},.T.)"))
                else:
                    oes.append(add(f"ORIENTED_EDGE('',*,*,{edges[(q, p)]},.F.)"))
            loop = add("EDGE_LOOP('',(" + ",".join(oes) + "))")
            bound = add(f"FACE_OUTER_BOUND('',{loop},.T.)")
            n = [0.0, 0.0, 0.0]
            n[ax] = 1.0 if side else -1.0
            r = [0.0, 0.0, 0.0]
            r[u] = 1.0
            place = add(f"AXIS2_PLACEMENT_3D('',{pt(corner[cs[0]])},{dirn(n)},{dirn(r)})")
            plane = add(f"PLANE('',{place})")
            faces.append(add(f"ADVANCED_FACE('',({bound}),{plane},.T.)"))
    shell = add("CLOSED_SHELL('',(" + ",".join(faces) + "))")
    solid = add(f"MANIFOLD_SOLID_BREP('box',{shell})")
    length = add("(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT("
                 + (f".{unit}." if unit else "$") + ",.METRE.))")
    angle = add("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
    solid_angle = add("(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())")
    unc = add(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),{length},"
              "'distance_accuracy_value','confusion accuracy')")
    ctx = add(f"(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT(({unc}))"
              f"GLOBAL_UNIT_ASSIGNED_CONTEXT(({length},{angle},{solid_angle}))"
              "REPRESENTATION_CONTEXT('Context #1','3D Context with UNIT and UNCERTAINTY'))")
    origin = add(f"AXIS2_PLACEMENT_3D('',{pt((0, 0, 0))},{dirn((0, 0, 1))},{dirn((1, 0, 0))})")
    rep = add(f"ADVANCED_BREP_SHAPE_REPRESENTATION('',({origin},{solid}),{ctx})")
    app = add("APPLICATION_CONTEXT('core data for automotive mechanical design processes')")
    add(f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,"
        f"{app})")
    pctx = add(f"PRODUCT_CONTEXT('',{app},'mechanical')")
    prod = add(f"PRODUCT('box','box','',({pctx}))")
    form = add(f"PRODUCT_DEFINITION_FORMATION('','',{prod})")
    dctx = add(f"PRODUCT_DEFINITION_CONTEXT('part definition',{app},'design')")
    pdef = add(f"PRODUCT_DEFINITION('design','',{form},{dctx})")
    pshape = add(f"PRODUCT_DEFINITION_SHAPE('','',{pdef})")
    add(f"SHAPE_DEFINITION_REPRESENTATION({pshape},{rep})")
    body = "\n".join(f"#{i}={t};" for i, t in enumerate(ents, start=1))
    path.write_text(
        "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('Air-Construct test'),'2;1');\n"
        "FILE_NAME('box.step','2026-01-01T00:00:00',(''),(''),'','','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n"
        + body + "\nENDSEC;\nEND-ISO-10303-21;\n")
    return path
