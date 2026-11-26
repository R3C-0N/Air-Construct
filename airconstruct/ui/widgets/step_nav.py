"""Pill-shaped step navigation of the top bar (Modèle, Air, ... with alert badges).

Owned by: workstream "ui" (shared widget, see docs/ui-architecture.md). Mirrors the
QTabBar calls the shell uses: addTab, count, tabText, currentIndex, setCurrentIndex,
currentChanged.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton

from ..theme import COLORS


class StepButton(QPushButton):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.setObjectName("step")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 10, 0)
        lay.setSpacing(7)
        self.label = QLabel(text)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.setStyleSheet("background: transparent; color: inherit;")
        self.badge = QLabel()
        self.badge.setObjectName("stepBadge")
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedHeight(18)
        self.badge.setMinimumWidth(18)
        self.badge.setVisible(False)
        lay.addWidget(self.label)
        lay.addWidget(self.badge)
        self._level: str | None = None
        self.toggled.connect(lambda _on: self._restyle())

    def set_badge(self, count: int, level: str | None, tooltip: str = "") -> None:
        self._level = level if count else None
        self.badge.setText(str(count) if count else "")
        self.badge.setVisible(bool(count))
        self.setToolTip(tooltip)
        self._restyle()
        self.updateGeometry()

    def _restyle(self) -> None:
        on = self.isChecked()
        self.label.setStyleSheet(
            f"background: transparent; color: {'white' if on else COLORS['text_secondary']};"
            f" font-weight: {600 if on else 500};")
        if self._level is None:
            return
        if on:
            bg, fg = "white", COLORS["accent"]
        else:
            bg, fg = (COLORS["error"], "white") if self._level == "error" else (
                COLORS["warning"], "white")
        self.badge.setStyleSheet(f"background: {bg}; color: {fg};")

    def sizeHint(self):
        return self.layout().sizeHint().expandedTo(super().sizeHint())


class StepNav(QFrame):
    currentChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("stepNav")
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(4, 4, 4, 4)
        self._lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idToggled.connect(self._on_toggled)
        self.buttons: list[StepButton] = []

    def addTab(self, text: str) -> int:
        b = StepButton(text)
        i = len(self.buttons)
        self.buttons.append(b)
        self._group.addButton(b, i)
        self._lay.addWidget(b)
        if i == 0:
            b.setChecked(True)
        return i

    def count(self) -> int:
        return len(self.buttons)

    def tabText(self, i: int) -> str:
        return self.buttons[i].label.text()

    def currentIndex(self) -> int:
        return self._group.checkedId()

    def setCurrentIndex(self, i: int) -> None:
        if 0 <= i < len(self.buttons):
            self.buttons[i].setChecked(True)

    def _on_toggled(self, i: int, checked: bool) -> None:
        if checked:
            self.currentChanged.emit(i)
