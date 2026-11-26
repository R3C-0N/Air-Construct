"""Selection shared by every view (object tree, 3D viewport, inspectors, alerts).

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

A selected object is named by a string key:

- ``"enclosure"``
- ``"component:<name>"``
- ``"opening:<name>"``

New object kinds (fan, grille, wall...) add their own ``"<kind>:<name>"`` prefix.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QObject, Signal

ENCLOSURE = "enclosure"


def component_key(name: str) -> str:
    return f"component:{name}"


def opening_key(name: str) -> str:
    return f"opening:{name}"


def parse_key(key: str) -> tuple[str, str | None]:
    """``"component:cpu"`` -> ``("component", "cpu")``; ``"enclosure"`` -> ``("enclosure", None)``."""
    kind, sep, name = key.partition(":")
    return (kind, name if sep else None)


def existing_keys(data: dict) -> set[str]:
    """Every key that names an object of the project data."""
    keys = {ENCLOSURE}
    keys |= {component_key(c.get("name")) for c in data.get("components") or []}
    keys |= {opening_key(o.get("name")) for o in data.get("openings") or []}
    return keys


class Selection(QObject):
    """Ordered list of selected keys. ``current`` is the last one clicked."""

    changed = Signal(list)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._keys: list[str] = []

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    @property
    def current(self) -> str | None:
        return self._keys[-1] if self._keys else None

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def set(self, keys: Iterable[str]) -> None:
        new = list(dict.fromkeys(k for k in keys if k))
        if new != self._keys:
            self._keys = new
            self.changed.emit(self.keys)

    def select(self, key: str | None, additive: bool = False) -> None:
        """Click behaviour: replace the selection, or toggle ``key`` when additive."""
        if key is None:
            if not additive:
                self.clear()
            return
        if not additive:
            self.set([key])
        elif key in self._keys:
            self.set([k for k in self._keys if k != key])
        else:
            self.set([*self._keys, key])

    def clear(self) -> None:
        self.set([])

    def rename(self, old: str, new: str) -> None:
        """Keep the selection when an object is renamed."""
        if old in self._keys:
            self.set([new if k == old else k for k in self._keys])

    def prune(self, data: dict) -> None:
        """Drop keys of objects that no longer exist (after a delete or undo)."""
        valid = existing_keys(data)
        self.set([k for k in self._keys if k in valid or parse_key(k)[0] not in
                  {"enclosure", "component", "opening"}])
