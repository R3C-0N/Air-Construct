"""The 3D viewport shared by every step tab.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

There is ONE viewport (one OpenGL context) for the whole window: tabs do not create
their own. A tab says what to draw through :meth:`StepTab.scene` and may put its
own controls above the view with :meth:`StepTab.viewport_toolbar`. The viewport
draws the selected objects highlighted and turns clicks into selection changes.
"""

from __future__ import annotations

import dataclasses

from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QToolButton, QVBoxLayout, QWidget

from .selection import Selection
from .view import CAMERAS, SceneItem, SceneView

HIGHLIGHT = "#1D4ED8"


def highlight_items(items: list[SceneItem], keys: list[str]) -> list[SceneItem]:
    """Copies of ``items`` with the selected ones outlined in the highlight colour."""
    selected = set(keys)
    out = []
    for it in items:
        if it.key is None or it.key not in selected:
            out.append(it)
            continue
        kw = dict(it.kwargs)
        if it.style == "wireframe":
            out.append(dataclasses.replace(it, color=HIGHLIGHT, kwargs={**kw, "line_width": 3}))
        else:
            kw.update(show_edges=True, edge_color=HIGHLIGHT, line_width=3)
            out.append(dataclasses.replace(it, kwargs=kw, opacity=max(it.opacity, 0.8)))
    return out


class Viewport(QWidget):
    """SceneView + camera buttons + a slot for the active tab's toolbar."""

    def __init__(self, selection: Selection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.selection = selection
        self.scene = SceneView()
        self.items: list[SceneItem] = []  # as given by the tab, before highlighting

        self.tab_toolbars = QStackedWidget()
        self.tab_toolbars.addWidget(QWidget())  # empty slot
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        bar.addWidget(self.tab_toolbars, 1)
        self.camera_buttons: dict[str, QToolButton] = {}
        for key, label in CAMERAS.items():
            b = QToolButton()
            b.setText(label)
            b.setToolTip(f"Vue {label.lower()}")
            b.clicked.connect(lambda _=False, k=key: self.scene.set_camera(k))
            bar.addWidget(b)
            self.camera_buttons[key] = b

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(bar)
        layout.addWidget(self.scene, 1)

        self.scene.picked.connect(self._on_picked)
        selection.changed.connect(lambda _keys: self._redraw())

    def set_tab_toolbar(self, widget: QWidget | None) -> None:
        if widget is None:
            self.tab_toolbars.setCurrentIndex(0)
            return
        if self.tab_toolbars.indexOf(widget) < 0:
            self.tab_toolbars.addWidget(widget)
        self.tab_toolbars.setCurrentWidget(widget)

    def show_items(self, items: list[SceneItem], reset_camera: bool = False) -> None:
        self.items = list(items)
        self._redraw(reset_camera)

    def _redraw(self, reset_camera: bool = False) -> None:
        self.scene.draw(highlight_items(self.items, self.selection.keys), reset_camera)

    def _on_picked(self, key, additive: bool) -> None:
        self.selection.select(key, additive)

    def close_plotter(self) -> None:
        self.scene.close_plotter()
