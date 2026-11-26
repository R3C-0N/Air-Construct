"""Base class of the five step tabs (Modèle, Air, Volume d'air, Simulation, Résultats).

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

The window layout is the same for every step: a left panel, the shared 3D viewport
in the middle (with an optional tab toolbar above it) and a right panel. A tab
provides those panels; the window swaps them when the tab is opened. Everything
a tab needs (project, selection, units, alerts, viewport, runner, expert mode) is
on ``self.ctx``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..alerts import Alert
from ..context import AppContext
from ..view import SceneItem


class StepTab(QObject):
    key: ClassVar[str] = ""  # stable id, used by alerts and goto_tab
    title: ClassVar[str] = ""  # label of the tab

    #: ask the window to redraw the viewport (it calls :meth:`scene` if this tab is open)
    scene_changed = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.active = False

    # ---- panels: built once, on first use ---------------------------------
    def left_panel(self) -> QWidget | None:
        return None

    def right_panel(self) -> QWidget | None:
        return None

    def viewport_toolbar(self) -> QWidget | None:
        """Controls shown above the 3D view while this tab is open."""
        return None

    # ---- viewport ---------------------------------------------------------
    def scene(self) -> list[SceneItem] | None:
        """What the viewport draws while this tab is open; None = the model geometry."""
        return None

    # ---- checks, actions --------------------------------------------------
    def alerts(self, project) -> Iterable[Alert]:
        """This step's checks; they set the tab badge and the status bar count."""
        return ()

    def actions(self) -> list[QAction]:
        """Keyboard shortcuts of this tab (active only while it is open); they are
        listed in the shortcuts dialog."""
        return []

    # ---- life cycle -------------------------------------------------------
    def activated(self) -> None:
        """The tab was opened."""

    def deactivated(self) -> None:
        """Another tab was opened."""


def placeholder(text: str) -> QWidget:
    """A panel saying what will come here."""
    w = QWidget()
    lay = QVBoxLayout(w)
    label = QLabel(text)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    label.setObjectName("placeholder")
    label.setStyleSheet("color: #6B7280;")
    lay.addWidget(label)
    lay.addStretch(1)
    return w
