"""Step 3, Volume d'air: mesh precision, mesh checks and preview.

Owned by: thread "Volume d'air" (see docs/ui-architecture.md). Foundation state:
placeholders.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ..base import StepTab, placeholder


class VolumeTab(StepTab):
    key = "volume"
    title = "Volume d’air"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._left = placeholder(
            "Étape Volume d’air (à venir) : précision du maillage, nombre de mailles, "
            "temps estimé et contrôles du maillage."
        )

    def left_panel(self) -> QWidget:
        return self._left
