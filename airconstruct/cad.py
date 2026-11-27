"""CAD import: read STL / STEP files into triangle surfaces, store them in the case,
fit them onto a component's box and voxelize them on the structured mesh.

Owned by: workstream "cad" (v1.1.0). No Qt here: everything is tested directly.

A component may carry a ``shape`` (:class:`airconstruct.case.Shape`): a closed
triangle surface kept in the case file itself (float32 triangles, zlib, base64), in
its own frame. The shape is fitted onto the component's box, scaled per axis from its
own bounding box, so the existing move / resize tools keep working: moving the box
moves the shape, resizing it stretches the shape. The mesh stays the structured grid
of :mod:`airconstruct.mesh`: the cells whose centre is inside the surface make the
part (a staircase approximation, finer with the precision level).

Readers:

- STL, ASCII (several ``solid`` blocks = several bodies) or binary;
- STEP (.step / .stp) through the optional ``cascadio`` package (OpenCascade), which
  tessellates the file to glTF binary; each part instance of the assembly becomes a
  body. glTF is in metres and Y-up: we turn it back to Z-up.

STL files carry no unit: the caller gives the scale (millimetres by default).
"""

from __future__ import annotations

import base64
import functools
import json
import re
import struct
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

STL_SUFFIXES = (".stl",)
STEP_SUFFIXES = (".step", ".stp")
SUFFIXES = STL_SUFFIXES + STEP_SUFFIXES
FILE_FILTER = "Fichiers CAO (*.step *.stp *.stl *.STEP *.STP *.STL)"

# unit of the file -> metres per unit
UNITS: dict[str, float] = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "in": 0.0254}

MAX_TRIANGLES = 400_000  # per body; larger surfaces make the case file heavy


class CadError(ValueError):
    """The file cannot be read (message in French, shown to the user)."""


@dataclass
class Body:
    """One closed (hopefully) surface read from a file, in metres."""

    name: str
    triangles: np.ndarray  # (n, 3, 3) float64

    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        pts = self.triangles.reshape(-1, 3)
        return pts.min(axis=0), pts.max(axis=0)

    @property
    def size(self) -> np.ndarray:
        lo, hi = self.bounds
        return hi - lo


# ----------------------------------------------------------------------- storage
def encode(triangles: np.ndarray) -> str:
    """Triangles (n, 3, 3) -> the text stored in the case file."""
    raw = np.ascontiguousarray(triangles, dtype="<f4").tobytes()
    return base64.b64encode(zlib.compress(raw, 6)).decode("ascii")


@functools.lru_cache(maxsize=64)
def _decode(data: str) -> np.ndarray:
    try:
        raw = zlib.decompress(base64.b64decode(data.encode("ascii"), validate=True))
    except (ValueError, zlib.error) as exc:
        raise ValueError(f"shape data is not valid ({exc})") from exc
    if len(raw) % 36:
        raise ValueError("shape data is not a list of triangles")
    tris = np.frombuffer(raw, dtype="<f4").reshape(-1, 3, 3).astype(np.float64)
    if not np.isfinite(tris).all():
        raise ValueError("shape data has non-finite coordinates")
    tris.setflags(write=False)
    return tris


def decode(data: str) -> np.ndarray:
    """The stored text -> triangles (n, 3, 3), read-only (cached)."""
    return _decode(data)


def fit(triangles: np.ndarray, lo, hi) -> np.ndarray:
    """Map the triangles from their own bounding box onto the box [lo, hi]."""
    t = np.asarray(triangles, dtype=np.float64)
    pts = t.reshape(-1, 3)
    smin, smax = pts.min(axis=0), pts.max(axis=0)
    span = smax - smin
    lo, hi = np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
    scale = np.where(span > 0, (hi - lo) / np.where(span > 0, span, 1.0), 0.0)
    out = lo + (pts - smin) * scale
    flat = span <= 0
    out[:, flat] = ((lo + hi) / 2)[flat]
    return out.reshape(t.shape)


def placed(shape_data: str, lo, hi) -> np.ndarray:
    """Triangles of a stored shape fitted onto the box [lo, hi]."""
    return fit(decode(shape_data), lo, hi)


# ----------------------------------------------------------------------- checks
def open_edges(triangles: np.ndarray) -> int:
    """Number of edges not shared by exactly two triangles (0 for a closed surface).
    Vertices are welded on a grid of 1e-6 of the size of the surface."""
    t = np.asarray(triangles, dtype=np.float64)
    if not len(t):
        return 0
    _, ids = _weld(t)
    e = np.concatenate([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]])
    e.sort(axis=1)
    e = e[e[:, 0] != e[:, 1]]
    _, counts = np.unique(e, axis=0, return_counts=True)
    return int((counts != 2).sum())


def _weld(t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    pts = t.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    tol = max(float((hi - lo).max()), 1e-12) * 1e-6
    q = np.round((pts - lo) / tol).astype(np.int64)
    uniq, inv = np.unique(q, axis=0, return_inverse=True)
    return uniq, inv.reshape(-1, 3)


def volume(triangles: np.ndarray) -> float:
    """Enclosed volume [m3] (divergence theorem; |.| so the orientation does not
    matter for a closed surface)."""
    t = np.asarray(triangles, dtype=np.float64)
    if not len(t):
        return 0.0
    return abs(float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum()) / 6.0)


def split_bodies(body: Body) -> list[Body]:
    """Split a surface made of several disconnected shells into one body each
    (an assembly exported as a single STL). Shells are sorted by size, largest first."""
    t = body.triangles
    if len(t) < 2:
        return [body]
    _, ids = _weld(t)
    label = np.arange(int(ids.max()) + 1)
    while True:  # label propagation through the triangles, with pointer jumping
        tri_min = label[ids].min(axis=1)
        new = label.copy()
        for k in range(3):
            np.minimum.at(new, ids[:, k], tri_min)
        new = new[new]
        if np.array_equal(new, label):
            break
        label = new
    roots = label[ids[:, 0]]
    uniq, inv = np.unique(roots, return_inverse=True)
    if len(uniq) == 1:
        return [body]
    parts = [t[inv == k] for k in range(len(uniq))]
    parts.sort(key=lambda p: -len(p))
    return [Body(f"{body.name}_{i + 1}", p) for i, p in enumerate(parts)]


# ----------------------------------------------------------------------- readers
def read(path: str | Path, unit: str = "mm") -> list[Body]:
    """Read a CAD file into bodies, in metres. ``unit`` (:data:`UNITS`) is the length
    unit of an STL file, which does not say; a STEP file gives its own unit."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in STL_SUFFIXES:
        bodies = read_stl(path)
        scale = UNITS[unit]
    elif suffix in STEP_SUFFIXES:
        bodies = read_step(path)
        scale = 1.0  # converted to metres by OpenCascade
    else:
        raise CadError(f"format non pris en charge : {path.suffix or path.name} "
                       "(STEP ou STL attendu)")
    out = []
    for b in bodies:
        if len(b.triangles) < 4:
            continue
        if len(b.triangles) > MAX_TRIANGLES:
            n = f"{len(b.triangles):,}".replace(",", " ")
            raise CadError(f"« {b.name} » a {n} triangles (maximum "
                           f"{MAX_TRIANGLES:,}".replace(",", " ")
                           + ") : exportez-le avec une tolérance plus grossière")
        out.append(Body(b.name, b.triangles * scale))
    if not out:
        raise CadError(f"aucune surface lisible dans {path.name}")
    return out


_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_VERTEX = re.compile(rf"vertex\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})")
_SOLID = re.compile(r"^\s*solid\b[ \t]*([^\r\n]*)$(.*?)^\s*endsolid\b", re.MULTILINE | re.DOTALL)


def read_stl(path: str | Path) -> list[Body]:
    """ASCII (one body per ``solid`` block) or binary STL, in the file's units."""
    path = Path(path)
    raw = path.read_bytes()
    stem = path.stem
    if _is_binary_stl(raw):
        n = struct.unpack_from("<I", raw, 80)[0]
        rec = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
        tris = np.frombuffer(raw, dtype=rec, count=n, offset=84)["v"].astype(np.float64)
        return [Body(stem, _clean(tris))]
    text = raw.decode("latin-1")
    bodies = []
    for i, m in enumerate(_SOLID.finditer(text)):
        verts = np.array(_VERTEX.findall(m.group(2)), dtype=np.float64)
        if len(verts) % 3:
            raise CadError(f"{path.name} : fichier STL incomplet")
        name = _safe_name(m.group(1).strip()) or (stem if i == 0 else f"{stem}_{i + 1}")
        bodies.append(Body(name, _clean(verts.reshape(-1, 3, 3))))
    if not bodies:
        raise CadError(f"{path.name} n’est pas un fichier STL lisible")
    if len(bodies) > 1 and len({b.name for b in bodies}) < len(bodies):
        bodies = [Body(f"{b.name}_{i + 1}", b.triangles) for i, b in enumerate(bodies)]
    return bodies


def _is_binary_stl(raw: bytes) -> bool:
    if len(raw) < 84:
        return False
    n = struct.unpack_from("<I", raw, 80)[0]
    if len(raw) == 84 + 50 * n:
        return True
    return raw.lstrip()[:5].lower() != b"solid"


def _clean(tris: np.ndarray) -> np.ndarray:
    """Drop non-finite and degenerate (zero area) triangles."""
    t = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
    t = t[np.isfinite(t).all(axis=(1, 2))]
    area2 = np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)
    return t[area2 > 0]


def _safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_")
    return s[:40]


def step_available() -> bool:
    try:
        import cascadio  # noqa: F401
    except ImportError:
        return False
    return True


def read_step(path: str | Path) -> list[Body]:
    """STEP -> bodies in metres, through cascadio (OpenCascade tessellation to GLB)."""
    path = Path(path)
    try:
        import cascadio
    except ImportError as exc:
        raise CadError("la lecture des fichiers STEP demande le module « cascadio » "
                       "(pip install cascadio). Exportez la pièce en STL en attendant."
                       ) from exc
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "shape.glb"
        try:
            cascadio.step_to_glb(str(path), str(out))
        except Exception as exc:
            raise CadError(f"{path.name} : lecture STEP impossible ({exc})") from exc
        if not out.is_file() or out.stat().st_size == 0:
            raise CadError(f"{path.name} : lecture STEP impossible (fichier vide ou invalide)")
        bodies = read_glb(out.read_bytes(), path.stem)
    # glTF is Y-up, CAD files are Z-up: (x, y, z)_gltf -> (x, -z, y)
    return [Body(b.name, b.triangles[..., [0, 2, 1]] * np.array([1.0, -1.0, 1.0]))
            for b in bodies]


_COMPONENTS = {5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def read_glb(raw: bytes, default_name: str = "piece") -> list[Body]:
    """glTF binary -> one body per mesh instance of the default scene (node transforms
    applied), coordinates as in the file. Only triangle primitives are read."""
    if len(raw) < 20 or raw[:4] != b"glTF":
        raise CadError("fichier glTF binaire invalide")
    off, js, binary = 12, None, b""
    while off + 8 <= len(raw):
        length, kind = struct.unpack_from("<II", raw, off)
        chunk = raw[off + 8: off + 8 + length]
        if kind == 0x4E4F534A:
            js = json.loads(chunk.decode("utf-8"))
        elif kind == 0x004E4942:
            binary = chunk
        off += 8 + length
    if js is None:
        raise CadError("fichier glTF binaire sans description")

    def accessor(i: int) -> np.ndarray:
        a = js["accessors"][i]
        bv = js["bufferViews"][a["bufferView"]]
        dt = np.dtype(_COMPONENTS[a["componentType"]])
        width = _WIDTH[a["type"]]
        start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = bv.get("byteStride") or dt.itemsize * width
        count = a["count"]
        buf = np.frombuffer(binary, dtype=np.uint8, count=bv["byteLength"] - a.get("byteOffset", 0),
                            offset=start)
        rows = np.lib.stride_tricks.as_strided(buf, shape=(count, dt.itemsize * width),
                                               strides=(stride, 1))
        return np.ascontiguousarray(rows).view(dt).reshape(count, width).astype(np.float64)

    def mesh_triangles(mi: int) -> np.ndarray:
        parts = []
        for prim in js["meshes"][mi].get("primitives", []):
            if prim.get("mode", 4) != 4 or "POSITION" not in prim.get("attributes", {}):
                continue
            pos = accessor(prim["attributes"]["POSITION"])[:, :3]
            idx = (accessor(prim["indices"])[:, 0].astype(np.int64) if "indices" in prim
                   else np.arange(len(pos)))
            idx = idx[: len(idx) // 3 * 3]
            parts.append(pos[idx].reshape(-1, 3, 3))
        return np.concatenate(parts) if parts else np.zeros((0, 3, 3))

    nodes = js.get("nodes", [])
    scenes = js.get("scenes") or [{"nodes": list(range(len(nodes)))}]
    roots = scenes[js.get("scene", 0)].get("nodes", [])
    bodies: list[Body] = []
    used: dict[str, int] = {}

    def visit(ni: int, parent_m: np.ndarray, parent_name: str) -> None:
        nd = nodes[ni]
        m = parent_m @ _node_matrix(nd)
        name = nd.get("name") or parent_name
        if "mesh" in nd:
            t = mesh_triangles(nd["mesh"])
            if len(t):
                pts = t.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
                mname = js["meshes"][nd["mesh"]].get("name")
                base = _safe_name(name or mname or default_name) or default_name
                used[base] = used.get(base, 0) + 1
                label = base if used[base] == 1 else f"{base}_{used[base]}"
                bodies.append(Body(label, _clean(pts.reshape(-1, 3, 3))))
        for c in nd.get("children", []):
            visit(c, m, name)

    for r in roots:
        visit(r, np.eye(4), "")
    if not bodies:
        raise CadError("aucune surface dans le fichier")
    return bodies


def _node_matrix(nd: dict) -> np.ndarray:
    if "matrix" in nd:
        return np.array(nd["matrix"], dtype=np.float64).reshape(4, 4).T  # column-major
    m = np.eye(4)
    x, y, z, w = nd.get("rotation", (0.0, 0.0, 0.0, 1.0))
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m[:3, :3] = r * np.array(nd.get("scale", (1.0, 1.0, 1.0)))
    m[:3, 3] = nd.get("translation", (0.0, 0.0, 0.0))
    return m


# ----------------------------------------------------------------------- parts
def component(body: Body, name: str, source: str | None = None,
              material: str = "aluminium") -> dict:
    """A component dict (project data) for the body, its box at the body's own
    position (metres, as read)."""
    lo, hi = body.bounds
    size = np.maximum(hi - lo, 1e-6)
    shape = {"data": encode(body.triangles), "triangles": len(body.triangles)}
    if source:
        shape["source"] = source
    return {"name": name, "material": material, "power": 0.0,
            "box": {"origin": {a: float(v) for a, v in zip("xyz", lo)},
                    "size": {a: float(v) for a, v in zip("xyz", size)}},
            "shape": shape}


# ----------------------------------------------------------------------- voxels
def voxelize(triangles: np.ndarray, xc, yc, zc) -> np.ndarray:
    """Which points of the grid xc x yc x zc (cell centres) are inside the closed
    surface: bool array (len(xc), len(yc), len(zc)).

    One ray along +z per (x, y) column, inside = odd number of crossings below the
    point. The rays are shifted by a tiny irrational amount so they do not run
    exactly through the edges and vertices of the triangles."""
    xc, yc, zc = (np.asarray(v, dtype=np.float64) for v in (xc, yc, zc))
    out = np.zeros((len(xc), len(yc), len(zc)), dtype=bool)
    t = np.asarray(triangles, dtype=np.float64)
    if not len(t) or not (len(xc) and len(yc) and len(zc)):
        return out
    pts = t.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    ext = max(float((hi - lo).max()), 1e-12)
    xr = xc + ext * 1.2345678e-7
    yr = yc + ext * 2.3456789e-7
    ax, ay = t[:, :, 0], t[:, :, 1]
    i0 = np.searchsorted(xr, ax.min(axis=1), "left")
    i1 = np.searchsorted(xr, ax.max(axis=1), "right")
    j0 = np.searchsorted(yr, ay.min(axis=1), "left")
    j1 = np.searchsorted(yr, ay.max(axis=1), "right")
    ni, nj = np.maximum(i1 - i0, 0), np.maximum(j1 - j0, 0)
    cnt = ni * nj
    total = int(cnt.sum())
    if total == 0:
        return out
    tri = np.repeat(np.arange(len(t)), cnt)
    off = np.arange(total) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    ii = i0[tri] + off // nj[tri]
    jj = j0[tri] + off % nj[tri]
    px, py = xr[ii], yr[jj]
    a, b, c = t[tri, 0], t[tri, 1], t[tri, 2]
    d = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    ok = d != 0
    with np.errstate(divide="ignore", invalid="ignore"):
        w0 = ((b[:, 0] - px) * (c[:, 1] - py) - (b[:, 1] - py) * (c[:, 0] - px)) / d
        w1 = ((c[:, 0] - px) * (a[:, 1] - py) - (c[:, 1] - py) * (a[:, 0] - px)) / d
    w2 = 1.0 - w0 - w1
    hit = ok & (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
    if not hit.any():
        return out
    zh = (w0 * a[:, 2] + w1 * b[:, 2] + w2 * c[:, 2])[hit]
    col = (ii * len(yc) + jj)[hit]
    # sort hits by (column, z) through one float key: column + z scaled into [0.25, 0.75]
    zlo, zspan = lo[2] - ext, 3 * ext
    key = col + 0.25 + 0.5 * (zh - zlo) / zspan
    key.sort()
    cols = np.unique(col)
    ci, cj = cols // len(yc), cols % len(yc)
    zq = 0.25 + 0.5 * (np.clip(zc, zlo, zlo + zspan) - zlo) / zspan
    q = cols[:, None] + zq[None, :]
    below = np.searchsorted(key, q, "left") - np.searchsorted(key, cols, "left")[:, None]
    out[ci, cj, :] = (below % 2) == 1
    return out
