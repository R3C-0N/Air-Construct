"""Alerts shown in the status bar and as badges on the step tabs.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

An :class:`Alert` belongs to a step tab (``tab``) and may point at an object
(``target``, a selection key): clicking it in the status bar opens that tab and
selects the object. Alerts come from providers, functions ``(project) -> alerts``
registered on :class:`AlertCenter`; every step tab's :meth:`StepTab.alerts` is one.
The built-in provider turns case validation errors into alerts.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pydantic import ValidationError
from PySide6.QtCore import QObject, Signal

from .selection import ENCLOSURE, component_key, opening_key

ERROR, WARNING, INFO = "error", "warning", "info"
LEVEL_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}

# tab keys (see tabs/__init__.py)
MODEL, AIR, VOLUME, SIMULATION, RESULTS = "model", "air", "volume", "simulation", "results"


@dataclass(frozen=True)
class Alert:
    level: str  # ERROR blocks the run, WARNING does not, INFO is a hint
    message: str
    tab: str | None = None
    target: str | None = None  # selection key of the object at fault


Provider = Callable[[object], Iterable[Alert]]


def _target_from_loc(loc: tuple, data: dict) -> str | None:
    if len(loc) >= 2 and loc[0] in ("components", "openings") and isinstance(loc[1], int):
        items = data.get(loc[0]) or []
        if 0 <= loc[1] < len(items) and isinstance(items[loc[1]], dict):
            name = items[loc[1]].get("name")
            if name:
                return component_key(name) if loc[0] == "components" else opening_key(name)
    if loc and loc[0] == "enclosure":
        return ENCLOSURE
    return None


_TAB_BY_FIELD = {
    "components": MODEL, "enclosure": MODEL, "name": MODEL, "fluid": AIR,
    "openings": AIR, "ambient_temperature": AIR, "ambient_pressure": AIR,
    "precision": VOLUME, "solver": SIMULATION,
}


#: French text of the whole-case checks (Case._check_geometry). A tab that checks the
#: same thing itself can reuse these strings: equal alerts are shown once.
CASE_MESSAGES = {
    "the case needs at least one inlet":
        "Aucune entrée d’air : ajoutez une entrée ou un ventilateur de soufflage.",
    "the case needs at least one outlet":
        "Aucune sortie d’air : ajoutez une sortie ou un ventilateur d’extraction.",
    "every opening has an imposed flow rate: at least one must be a free vent, a plain "
    "outlet or a fan with a curve":
        "Toutes les ouvertures imposent leur débit : il en faut au moins une libre "
        "(sortie à pression ambiante, entrée libre ou ventilateur avec courbe).",
}


def validation_alerts(data: dict, error: Exception | None) -> list[Alert]:
    """Case validation errors, as alerts attached to the right tab and object."""
    if error is None:
        return []
    if not isinstance(error, ValidationError):
        return [Alert(ERROR, str(error), MODEL)]
    out = []
    for e in error.errors():
        loc = tuple(e.get("loc", ()))
        msg = str(e.get("msg", "")).removeprefix("Value error, ")
        if loc:
            out.append(Alert(ERROR, f"{'.'.join(map(str, loc))} : {msg}",
                             _TAB_BY_FIELD.get(str(loc[0]), MODEL), _target_from_loc(loc, data)))
            continue
        # whole-case checks (Case._check_geometry): find the tab and object from the text
        tab = AIR if re.search(r"\b(inlet|outlet|opening)\b", msg) else MODEL
        target = None
        m = re.search(r"component '([^']+)'", msg)
        if m:
            target = component_key(m.group(1))
        out.append(Alert(ERROR, CASE_MESSAGES.get(msg, msg), tab, target))
    return out


class AlertCenter(QObject):
    """Collects alerts from every provider; recomputed when the project changes."""

    changed = Signal()

    def __init__(self, project, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self._providers: list[Provider] = [
            lambda p: validation_alerts(p.get() or {}, p.validation_error)
        ]
        self._alerts: list[Alert] = []
        project.changed.connect(self.refresh)
        project.results_changed.connect(self.refresh)

    def register(self, provider: Provider) -> None:
        self._providers.append(provider)
        self.refresh()

    def refresh(self) -> None:
        alerts: list[Alert] = []
        for provider in self._providers:
            try:
                alerts.extend(provider(self.project) or ())
            except Exception as exc:  # noqa: BLE001 - a broken check must not break the UI
                alerts.append(Alert(WARNING, f"Contrôle en échec : {exc}"))
        alerts = list(dict.fromkeys(alerts))
        alerts.sort(key=lambda a: LEVEL_ORDER.get(a.level, 9))
        if alerts != self._alerts:
            self._alerts = alerts
            self.changed.emit()

    @property
    def alerts(self) -> list[Alert]:
        return list(self._alerts)

    def for_tab(self, tab: str) -> list[Alert]:
        return [a for a in self._alerts if a.tab == tab]

    def for_target(self, key: str) -> list[Alert]:
        return [a for a in self._alerts if a.target == key]

    @property
    def has_errors(self) -> bool:
        return any(a.level == ERROR for a in self._alerts)
