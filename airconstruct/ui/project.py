"""The project being edited: case data, file, undo/redo history and last results.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

The source of truth is a plain dict (``Project.data``) in the shape of
``Case.model_dump(mode="json")``. It may be temporarily invalid while the user edits
(no outlet yet, a part outside the box...): ``Project.case`` is then ``None`` and
``Project.validation_error`` says why, the alerts show it, and the last valid case
stays on screen. Every change goes through :meth:`Project.edit` or
:meth:`Project.set_data`, which push one undoable step on ``Project.undo_stack``.

Keys the :class:`~airconstruct.case.Case` model does not know are kept in the dict
and in the saved file (pydantic ignores them when validating). UI-only metadata
(hidden or locked parts, groups, tab preferences...) goes under ``data["ui"]``; it
does not make results stale.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack

from ..case import Case

UI_KEY = "ui"
MERGE_WINDOW_S = 1.0  # edits with the same merge key closer than this make one undo step

_MERGE_IDS: dict[str, int] = {}


def _merge_id(key: str | None) -> int:
    if key is None:
        return -1
    return _MERGE_IDS.setdefault(key, len(_MERGE_IDS) + 1)


def physics_part(data: dict) -> dict:
    """The part of the data that changes the simulation (everything but ``ui``)."""
    return {k: v for k, v in data.items() if k != UI_KEY}


@dataclass
class RunRecord:
    """A finished run: its outcome and the exact project data it was run on."""

    outcome: object  # worker.RunOutcome
    case: Case
    data: dict


class _EditCommand(QUndoCommand):
    def __init__(self, project: Project, label: str, before: dict, after: dict,
                 merge_key: str | None) -> None:
        super().__init__(label)
        self.project = project
        self.before = before
        self.after = after
        self.merge_key = merge_key
        self.stamp = time.monotonic()

    def id(self) -> int:
        return _merge_id(self.merge_key)

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, _EditCommand) or other.merge_key != self.merge_key:
            return False
        if other.stamp - self.stamp > MERGE_WINDOW_S:
            return False
        self.after = other.after
        self.stamp = other.stamp
        return True

    def redo(self) -> None:
        self.project._apply(self.after)

    def undo(self) -> None:
        self.project._apply(self.before)


class Project(QObject):
    """Editable, serialisable case + history. One instance lives for the whole window;
    New/Open replace its content (``replaced`` is emitted, then ``changed``)."""

    changed = Signal()  # data changed: an edit, undo/redo, new or open
    replaced = Signal()  # a different project was loaded (history cleared)
    dirty_changed = Signal(bool)
    path_changed = Signal()
    results_changed = Signal()  # a run finished or results were cleared
    stale_changed = Signal(bool)  # results no longer match the model

    def __init__(self, data: dict | None = None, path: Path | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.undo_stack = QUndoStack(self)
        self.undo_stack.cleanChanged.connect(self._on_clean_changed)
        self._data: dict = copy.deepcopy(data) if data is not None else {}
        self._path: Path | None = Path(path) if path else None
        self._case: Case | None = None
        self._error: Exception | None = None
        self._validated = False
        self._results: RunRecord | None = None
        self._stale = False
        self._last_valid: Case | None = None

    def _on_clean_changed(self, clean: bool) -> None:
        self.dirty_changed.emit(not clean)

    # ------------------------------------------------------------------ data
    @property
    def data(self) -> dict:
        """A copy of the current data: mutate it and pass it to :meth:`set_data`."""
        return copy.deepcopy(self._data)

    def get(self, *keys, default=None):
        """Read one value without copying everything, e.g. ``get("enclosure", "x")``."""
        cur = self._data
        for k in keys:
            try:
                cur = cur[k]
            except (KeyError, IndexError, TypeError):
                return default
        return copy.deepcopy(cur)

    def _validate(self) -> None:
        if self._validated:
            return
        self._validated = True
        try:
            self._case = Case.model_validate(self._data)
            self._error = None
            self._last_valid = self._case
        except Exception as exc:  # noqa: BLE001 - pydantic.ValidationError or ValueError
            self._case = None
            self._error = exc

    @property
    def case(self) -> Case | None:
        """The validated Case, or None while the data is invalid."""
        self._validate()
        return self._case

    @property
    def validation_error(self) -> Exception | None:
        self._validate()
        return self._error

    @property
    def last_valid_case(self) -> Case | None:
        """The current case if valid, else the last valid one (to keep drawing it)."""
        self._validate()
        return self._last_valid

    @property
    def name(self) -> str:
        return str(self._data.get("name") or "sans_nom")

    def edit(self, label: str, fn: Callable[[dict], object],
             merge_key: str | None = None) -> bool:
        """Let ``fn`` modify a copy of the data in place (its return value is ignored);
        the result is one undoable step named ``label``. Consecutive edits with the same ``merge_key``
        within a second merge into one step (typing, dragging). Returns False when
        nothing changed."""
        new = copy.deepcopy(self._data)
        fn(new)
        return self.set_data(label, new, merge_key)

    def set_data(self, label: str, data: dict, merge_key: str | None = None) -> bool:
        if data == self._data:
            return False
        self.undo_stack.push(_EditCommand(self, label, copy.deepcopy(self._data),
                                          copy.deepcopy(data), merge_key))
        return True

    def _apply(self, data: dict) -> None:
        self._data = copy.deepcopy(data)
        self._validated = False
        self.changed.emit()
        self._update_stale()

    # ------------------------------------------------------------------ files
    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def dirty(self) -> bool:
        return not self.undo_stack.isClean()

    def reset(self, data: dict, path: Path | None = None) -> None:
        """Replace the whole project (New / Open): history and results are cleared."""
        self.undo_stack.clear()
        self._path = Path(path) if path else None
        self._results = None
        self._last_valid = None
        self._data = copy.deepcopy(data)
        self._validated = False
        self.undo_stack.setClean()
        self.replaced.emit()
        self.path_changed.emit()
        self.dirty_changed.emit(False)
        self.results_changed.emit()
        self.changed.emit()
        self._update_stale()

    def reset_case(self, case: Case, path: Path | None = None) -> None:
        self.reset(case.model_dump(mode="json"), path)

    @staticmethod
    def read_file(path: str | Path) -> dict:
        """Read a project file. Raises ValueError if it is not a mapping."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an Air-Construct case file")  # noqa: TRY004
        return data

    def load(self, path: str | Path) -> None:
        """Open a file. Invalid cases are opened too (the alerts say what is wrong)."""
        self.reset(self.read_file(path), Path(path))

    def to_yaml(self) -> str:
        return yaml.safe_dump(self._data, sort_keys=False, allow_unicode=True)

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self._path
        if target is None:
            raise ValueError("no file name")
        target.write_text(self.to_yaml(), encoding="utf-8")
        if target != self._path:
            self._path = target
            self.path_changed.emit()
        self.undo_stack.setClean()
        return target

    # ------------------------------------------------------------------ results
    @property
    def results(self) -> RunRecord | None:
        return self._results

    def set_results(self, outcome, case: Case, data: dict) -> None:
        self._results = RunRecord(outcome, case, copy.deepcopy(data))
        self.results_changed.emit()
        self._update_stale()

    def clear_results(self) -> None:
        if self._results is not None:
            self._results = None
            self.results_changed.emit()
            self._update_stale()

    @property
    def stale(self) -> bool:
        """True when results exist but the model changed since they were computed."""
        return self._stale

    def _update_stale(self) -> None:
        stale = (self._results is not None
                 and physics_part(self._results.data) != physics_part(self._data))
        if stale != self._stale:
            self._stale = stale
            self.stale_changed.emit(stale)
