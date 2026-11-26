"""On/off switch (the "Mode expert" toggle of the mockup): a QCheckBox drawn as a pill.

Owned by: workstream "ui" (shared widget, see docs/ui-architecture.md).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QCheckBox

from ..theme import COLORS


class ToggleSwitch(QCheckBox):
    TRACK_W, TRACK_H, GAP = 32, 18, 10

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        w = self.TRACK_W + self.GAP + fm.horizontalAdvance(self.text()) + 4
        return QSize(w, max(self.TRACK_H + 6, fm.height() + 6))

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = (self.height() - self.TRACK_H) / 2
        track = QRectF(0, y, self.TRACK_W, self.TRACK_H)
        on = self.isChecked()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COLORS["accent"] if on else COLORS["input_border"]))
        p.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        knob = self.TRACK_H - 4
        x = track.right() - knob - 2 if on else track.left() + 2
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, y + 2, knob, knob))
        p.setPen(QColor(COLORS["text"]))
        f = self.font()
        f.setWeight(f.Weight.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(self.TRACK_W + self.GAP, 0, self.width(), self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        p.end()
