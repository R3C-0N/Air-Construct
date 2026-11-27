"""Edits of openings and walls on the raw project data (no Qt), used by the Air tab.

Owned by: thread "Air". Every function changes the dict it is given in place, so it
can run inside ``project.edit(label, fn)``.
"""

from __future__ import annotations

from .geometry import default_size, free_spot, unique_name

M3H = 3600.0  # m3/s -> m3/h
# a generic 60 mm axial fan (m3/s, Pa), as in examples/fan_box.yaml
DEFAULT_CURVE = [[0.0, 25.0], [0.004, 18.0], [0.008, 8.0], [0.011, 0.0]]
DEFAULT_VELOCITY = 1.0  # m/s, for a new inlet with an imposed flow

INLET_MODES = {"flow": "Débit imposé", "velocity": "Vitesse imposée",
               "fan": "Ventilateur", "free": "Libre (pression ambiante)"}
OUTLET_MODES = {"pressure": "Libre (pression ambiante)", "fan": "Ventilateur d’extraction"}
EXPERT_MODES = {"velocity"}

WALL_MODES = {"adiabatic": "Adiabatique (isolée)", "exchange": "Échange avec l’extérieur"}
H_PRESETS = [(5.0, "Air calme (convection naturelle)"), (10.0, "Air peu agité"),
             (25.0, "Air ventilé"), (50.0, "Soufflage fort")]
WALL_DEFAULTS = {"mode": "adiabatic", "material": "aluminium", "thickness": 0.001,
                 "h_external": 5.0, "t_external": None, "emissivity": 0.0}


def find_opening(data: dict, name: str) -> dict | None:
    return next((o for o in data.get("openings") or [] if o.get("name") == name), None)


def drive_mode(o: dict) -> str:
    if o.get("fan"):
        return "fan"
    if o.get("kind") == "outlet":
        return "pressure"
    if o.get("velocity") is not None:
        return "velocity"
    if o.get("flow_rate") is not None:
        return "flow"
    return "free"


def area(o: dict) -> float:
    try:
        return float(o["size"][0]) * float(o["size"][1])
    except (KeyError, TypeError, IndexError, ValueError):
        return 0.0


def imposed_flow(o: dict) -> float | None:
    """Imposed volumetric flow [m3/s] (flow rate, velocity x area, or fixed fan flow)."""
    if o.get("flow_rate") is not None:
        return float(o["flow_rate"])
    if o.get("velocity") is not None:
        return float(o["velocity"]) * area(o)
    fan = o.get("fan") or {}
    if fan.get("flow_rate") is not None:
        return float(fan["flow_rate"])
    return None


def set_drive(o: dict, mode: str) -> None:
    """Switch what drives the flow through an opening, keeping the flow rate when it
    can (a flow rate becomes a velocity and the other way round)."""
    current = imposed_flow(o)
    a = area(o) or 1e-6
    for k in ("flow_rate", "velocity"):
        o.pop(k, None)
        o[k] = None
    if mode != "fan":
        o["fan"] = None
    q = current if current is not None else DEFAULT_VELOCITY * a
    if mode == "flow":
        o["flow_rate"] = q
    elif mode == "velocity":
        o["velocity"] = q / a
    elif mode == "fan":
        if not o.get("fan"):
            o["fan"] = {"flow_rate": None, "curve": [list(p) for p in DEFAULT_CURVE]}
    if o.get("kind") == "outlet":
        o["flow_rate"] = o["velocity"] = None


def set_kind(o: dict, kind: str) -> None:
    o["kind"] = kind
    if kind == "outlet":
        o["flow_rate"] = o["velocity"] = None
        o["temperature"] = None


def set_fan_spec(o: dict, spec: str) -> None:
    """``"curve"`` or ``"flow"`` for a fan already on the opening."""
    fan = dict(o.get("fan") or {})
    if spec == "curve":
        q = fan.get("flow_rate")
        fan["flow_rate"] = None
        if not fan.get("curve"):
            fan["curve"] = [list(p) for p in DEFAULT_CURVE]
            if q:  # scale the generic curve so its free flow is ~1.6 x the old flow
                f = 1.6 * q / DEFAULT_CURVE[-1][0]
                fan["curve"] = [[p[0] * f, p[1]] for p in DEFAULT_CURVE]
    else:
        curve = fan.get("curve") or DEFAULT_CURVE
        fan["flow_rate"] = fan.get("flow_rate") or 0.6 * float(curve[-1][0]) or 0.005
        fan["curve"] = None
    o["fan"] = fan


def set_grille(o: dict, on: bool, open_area: float = 0.5) -> None:
    o["grille"] = {"open_area": open_area, "loss_coefficient": None} if on else None


def new_opening(data: dict, kind: str, face: str, fan: bool = False,
                prefer: tuple[float, float] | None = None) -> dict:
    """Add an opening on ``face`` at a free spot (near ``prefer``) and return it."""
    size = default_size(data, face, fan)
    center = free_spot(data, face, size, prefer)
    base = "fan" if fan else kind
    o = {"name": unique_name(data, base), "kind": kind, "face": face,
         "center": [center[0], center[1]], "size": [size[0], size[1]],
         "flow_rate": None, "velocity": None, "temperature": None, "fan": None,
         "grille": None}
    if fan:
        set_drive(o, "fan")
    elif kind == "inlet":
        o["flow_rate"] = round(DEFAULT_VELOCITY * size[0] * size[1], 6)
    data.setdefault("openings", []).append(o)
    return o


def rename_opening(data: dict, old: str, new: str) -> None:
    o = find_opening(data, old)
    if o is not None:
        o["name"] = new


def delete_opening(data: dict, name: str) -> None:
    data["openings"] = [o for o in data.get("openings") or [] if o.get("name") != name]


def move_to_face(data: dict, o: dict, face: str) -> None:
    """Put an opening on another face, keeping its size if it fits."""
    from .geometry import clamp_center, face_size

    w, h = face_size(data, face)
    size = (min(float(o["size"][0]), w), min(float(o["size"][1]), h))
    o["face"] = face
    o["size"] = [size[0], size[1]]
    c = clamp_center((float(o["center"][0]), float(o["center"][1])), size, w, h)
    others = [p for p in data.get("openings") or [] if p is not o]
    probe = {**data, "openings": others}
    c = free_spot(probe, face, size, c)
    o["center"] = [c[0], c[1]]


# ---------------------------------------------------------------- walls
def walls(data: dict) -> dict:
    w = data.get("walls")
    if not isinstance(w, dict):
        w = {}
        data["walls"] = w
    for k, v in WALL_DEFAULTS.items():
        w.setdefault(k, v)
    w.setdefault("faces", {})
    if w["faces"] is None:
        w["faces"] = {}
    return w


def wall_for_face(data: dict, face: str) -> dict:
    """The thermal setting of one face (its override, else the common one)."""
    w = data.get("walls") or {}
    faces = w.get("faces") or {}
    if face in faces:
        return {**WALL_DEFAULTS, **faces[face]}
    return {**WALL_DEFAULTS, **{k: v for k, v in w.items() if k != "faces"}}


def set_face_override(data: dict, face: str, override: dict | None) -> None:
    w = walls(data)
    if override is None:
        w["faces"].pop(face, None)
    else:
        w["faces"][face] = {**WALL_DEFAULTS, **override}


def wall_summary(setting: dict) -> str:
    if setting.get("mode") != "exchange":
        return "adiabatique"
    return (f"{setting.get('material', '')} · {float(setting.get('thickness') or 0) * 1000:g} mm"
            f" · h {float(setting.get('h_external') or 0):g} W/m²K")

