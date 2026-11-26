"""White rounded panel with an optional title row, as in the mockup.

Owned by: workstream "ui" (shared widget, see docs/ui-architecture.md).

    card = Card("Contrôles du modèle")
    card.body.addWidget(some_widget)
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class Card(QFrame):
    def __init__(self, title: str | None = None, parent: QWidget | None = None,
                 margins: tuple[int, int, int, int] = (12, 10, 12, 12)) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        self.header = QFrame()
        self.header.setObjectName("cardHeader")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(12, 10, 8, 8)
        hl.setSpacing(8)
        self.title_label = QLabel(title or "")
        self.title_label.setObjectName("cardTitle")
        hl.addWidget(self.title_label, 1)
        self.header_layout = hl  # add buttons on the right of the title here
        self.header.setVisible(bool(title))
        outer.addWidget(self.header)
        inner = QWidget()
        inner.setObjectName("cardBody")
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(8)
        outer.addWidget(inner, 1)

    @property
    def title(self) -> str:
        return self.title_label.text()

    def set_title(self, text: str | None) -> None:
        self.title_label.setText(text or "")
        self.header.setVisible(bool(text))
