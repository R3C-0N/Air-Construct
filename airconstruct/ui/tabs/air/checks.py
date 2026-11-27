"""Checks of the Air step (no Qt): openings that overflow their face or overlap each
other, a missing inlet or outlet, openings blocked by a part, fans that stall.

Owned by: thread "Air". They run on the raw data, so they still work while the case is
invalid (that is when they matter most).
"""

from __future__ import annotations

from ...alerts import AIR, CASE_MESSAGES, ERROR, WARNING, Alert
from ...selection import opening_key
from .geometry import (
    FACE_AXES,
    FACE_LABELS,
    component_footprints,
    face_size,
    opening_rect,
    openings_on,
)
from .ops import area, imposed_flow

MAX_VELOCITY = 10.0  # m/s through an opening: above this, noise and pressure loss explode
LOW_OPEN_AREA = 0.15


def _face(face: str) -> str:
    return FACE_LABELS.get(face, face).split(" (")[0].lower()


def air_alerts(data: dict, case=None) -> list[Alert]:
    """Alerts of the Air tab. ``case`` is the validated Case (or None) for the checks
    that need the flow estimate."""
    out: list[Alert] = []
    openings = [o for o in data.get("openings") or [] if isinstance(o, dict)]
    kinds = {o.get("kind") for o in openings}
    if "inlet" not in kinds:
        out.append(Alert(ERROR, CASE_MESSAGES["the case needs at least one inlet"], AIR))
    if "outlet" not in kinds:
        out.append(Alert(ERROR, CASE_MESSAGES["the case needs at least one outlet"], AIR))

    for face in FACE_AXES:
        w, h = face_size(data, face)
        on = [(o, opening_rect(o)) for o in openings_on(data, face)]
        on = [(o, r) for o, r in on if r is not None]
        for o, r in on:
            name = str(o.get("name"))
            if r.size[0] <= 0 or r.size[1] <= 0:
                out.append(Alert(ERROR, f"« {name} » a une taille nulle.", AIR,
                                 opening_key(name)))
            elif not r.inside(w, h):
                out.append(Alert(ERROR, f"« {name} » dépasse de la face {_face(face)} : "
                                        "réduisez-la ou recentrez-la.", AIR, opening_key(name)))
        for i, (a, ra) in enumerate(on):
            for b, rb in on[i + 1:]:
                if ra.overlap_area(rb) > 0:
                    out.append(Alert(ERROR, f"« {a.get('name')} » et « {b.get('name')} » se "
                                            f"recouvrent sur la face {_face(face)}.", AIR,
                                     opening_key(str(b.get("name")))))
        feet = component_footprints(data, face)
        for o, r in on:
            a = r.size[0] * r.size[1]
            blocked = sum(r.overlap_area(fr) for _, fr in feet)
            if a > 0 and blocked > 0.5 * a:
                who = ", ".join(n for n, fr in feet if r.overlap_area(fr) > 0)
                out.append(Alert(WARNING, f"« {o.get('name')} » est en grande partie bouchée "
                                          f"par {who}.", AIR, opening_key(str(o.get("name")))))

    for o in openings:
        name = str(o.get("name"))
        q = imposed_flow(o)
        a = area(o)
        if q is not None and a > 0 and q / a > MAX_VELOCITY:
            v = f"{q / a:.1f}".replace(".", ",")
            out.append(Alert(WARNING, f"« {name} » : {v} m/s dans l’ouverture, c’est "
                                      "beaucoup (bruit, pertes). Agrandissez-la ?", AIR,
                             opening_key(name)))
        g = o.get("grille") or {}
        try:
            oa = float(g.get("open_area")) if g and g.get("loss_coefficient") is None else None
        except (TypeError, ValueError):
            oa = None
        if oa is not None and oa < LOW_OPEN_AREA:
            out.append(Alert(WARNING, f"« {name} » : grille ouverte à {oa * 100:.0f} % "
                                      "seulement, la perte de charge sera forte.", AIR,
                             opening_key(name)))

    if case is not None:
        out.extend(_flow_alerts(case))
    return out


def _flow_alerts(case) -> list[Alert]:
    from ....fans import estimate_flow

    out = []
    try:
        est = estimate_flow(case)
    except Exception:  # noqa: BLE001 - the estimate is a hint, never a blocker
        return out
    if est.flow_rate <= 0:
        out.append(Alert(WARNING, "Aucun débit imposé ni ventilateur : seul le tirage "
                                  "naturel fera circuler l’air.", AIR))
    for fp in est.fans:
        o = next((p for p in case.openings if p.name == fp.name), None)
        if o is None or not o.fan_curve:
            continue
        q_max = o.fan.curve[-1][0]
        if fp.flow_rate <= 0.05 * q_max:
            out.append(Alert(WARNING, f"« {o.name} » : le ventilateur est presque bloqué "
                                      f"({fp.pressure:.0f} Pa), le circuit est trop résistant.",
                             AIR, opening_key(o.name)))
    return out
