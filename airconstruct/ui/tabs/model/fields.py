"""Small input widgets of the Modèle inspector.

Owned by: thread "Modèle" (see docs/ui-architecture.md).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QWidget

from ...theme import set_prop


def parse_number(text: str) -> float | None:
    t = (text or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fmt_number(v: float | None, digits: int = 4) -> str:
    if v is None:
        return ""
    s = f"{v:.{digits}g}" if abs(v) < 10 ** digits else f"{v:.0f}"
    return s.replace(".", ",")


class NumberField(QWidget):
    """A number with its unit. Empty with a "mixte" placeholder when the selected
    parts differ. ``committed(value)`` is emitted when the user validates a new
    value (Enter or leaving the field); ``value`` None means "cleared"."""

    committed = Signal(object)

    def __init__(self, unit: str = "", minimum: float | None = None,
                 allow_empty: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.minimum = minimum
        self.allow_empty = allow_empty
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.edit = QLineEdit()
        self.edit.setMinimumWidth(56)
        self.unit_label = QLabel(unit)
        set_prop(self.unit_label, role="muted")
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.unit_label)
        self._shown = ""
        self.edit.editingFinished.connect(self._commit)

    def set_unit(self, unit: str) -> None:
        self.unit_label.setText(unit)

    def set_value(self, v: float | None, mixed: bool = False, digits: int = 4) -> None:
        self._shown = "" if mixed else fmt_number(v, digits)
        self.edit.setPlaceholderText("mixte" if mixed else ("—" if self.allow_empty else ""))
        if not self.edit.hasFocus() or self.edit.text() != self._shown:
            self.edit.setText(self._shown)

    def value(self) -> float | None:
        return parse_number(self.edit.text())

    def _commit(self) -> None:
        text = self.edit.text()
        if text == self._shown:
            return
        v = parse_number(text)
        if v is None and not (self.allow_empty and not text.strip()):
            self.edit.setText(self._shown)  # not a number: put the value back
            return
        if v is not None and self.minimum is not None and v < self.minimum:
            self.edit.setText(self._shown)
            return
        self._shown = text
        self.committed.emit(v)
