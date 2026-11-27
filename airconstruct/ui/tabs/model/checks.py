"""Live checks of the Modèle step: overlapping parts, parts out of the enclosure,
criteria that cannot be met.

Owned by: thread "Modèle" (see docs/ui-architecture.md).
"""

from __future__ import annotations

import re

from ...alerts import ERROR, MODEL, WARNING, Alert
from ...selection import component_key
from . import geometry as geo

AXIS_NAMES = "xyz"


def _mm(v: float) -> str:
    return f"{v * 1000:.3g}".replace(".", ",") + " mm"


def model_alerts(data: dict, validation_error: Exception | None = None) -> list[Alert]:
    out: list[Alert] = []
    for a, b, depth in geo.overlaps(data):
        axis = min(range(3), key=lambda i: depth[i])
        out.append(Alert(WARNING, f"{a} chevauche {b} (recouvrement de {_mm(depth[axis])} "
                         f"en {AXIS_NAMES[axis]})", MODEL, component_key(a)))
    # the case validation already reports the first part out of the box (in English):
    # do not report that one twice
    already = set(re.findall(r"component '([^']+)' is outside", str(validation_error or "")))
    for name, axis, by in geo.outside(data):
        if name not in already:
            out.append(Alert(ERROR, f"{name} dépasse de l’enceinte de {_mm(by)} en "
                             f"{AXIS_NAMES[axis]}", MODEL, component_key(name)))
    t_amb = data.get("ambient_temperature")
    for c in data.get("components") or []:
        tmax = c.get("max_temperature")
        try:
            if tmax is not None and t_amb is not None and float(tmax) <= float(t_amb):
                out.append(Alert(WARNING, f"{c.get('name')} : température max admissible "
                                 f"({float(tmax):g} °C) pas au-dessus de l’ambiante "
                                 f"({float(t_amb):g} °C)", MODEL, component_key(str(c.get('name')))))
        except (TypeError, ValueError):
            pass
    return out
