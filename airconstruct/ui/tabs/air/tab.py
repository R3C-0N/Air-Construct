"""Step 2, Air: openings (inlets, outlets), ambient conditions, quick estimate.

Owned by: thread "Air" (see docs/ui-architecture.md). Foundation state: the list of
openings (shared selection) and placeholders.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ...widgets.object_tree import ObjectTree
from ..base import StepTab, placeholder


class AirTab(StepTab):
    key = "air"
    title = "Air"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._left = QWidget()
        lay = QVBoxLayout(self._left)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(QLabel("<b>Air</b>"))
        lay.addWidget(ObjectTree(ctx, kinds=("opening",)), 1)
        self._right = placeholder(
            "Étape Air (à venir) : éditeur 2D des ouvertures sur une face, débit ou "
            "vitesse, température d’entrée, conditions ambiantes et estimation rapide."
        )

    def left_panel(self) -> QWidget:
        return self._left

    def right_panel(self) -> QWidget:
        return self._right
