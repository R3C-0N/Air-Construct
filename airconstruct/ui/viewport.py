"""The 3D viewport shared by every step tab.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

There is ONE viewport (one OpenGL context) for the whole window: tabs do not create
their own. A tab says what to draw through :meth:`StepTab.scene` and may put its
own controls above the view with :meth:`StepTab.viewport_toolbar`. The viewport
draws the selected objects highlighted and turns clicks into selection changes.

Mouse in the view: a click selects (Ctrl or Shift adds, a click on nothing clears),
a double click frames the object, a press and drag on an object the open tab can
move (:meth:`StepTab.drag`) moves it, anything else turns the camera. The open tab
may also take clicks for a tool of its own (:meth:`StepTab.click`, e.g. placing a
probe).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .selection import Selection
from .view import CAMERAS, Pick, SceneItem, SceneView

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
            out.append(dataclasses.replace(it, kwargs=kw, opacity=max(it.opacity, it.highlight_opacity)))
    return out


@dataclass
class Drag:
    """What a tab returns from :meth:`StepTab.drag` to move objects with the mouse.

    The mouse moves on a plane through the grabbed point; ``delta`` is how far it
    went from that point (metres, x y z) and ``free`` is True when Alt is held (no
    snapping)."""

    keys: list[str]  # selection keys of the items that follow the mouse
    finish: Callable[[tuple, bool], bool]  # (delta, free) -> True if the model changed
    constrain: Callable[[tuple, bool], tuple] | None = None  # snapped delta, shown live
    cancel: Callable[[], None] | None = None  # released without moving


class Viewport(QWidget):
    """SceneView + camera buttons + a slot for the active tab's toolbar.

    ``redrawn`` is emitted after every redraw: a tab that adds widgets to the scene
    (move/resize handles) re-adds them then."""

    redrawn = Signal()

    def __init__(self, selection: Selection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.selection = selection
        self.scene = SceneView()
        self.items: list[SceneItem] = []  # as given by the tab, before highlighting

        # the active tab's toolbar, on a card above the view (hidden when none)
        self.toolbar_card = QFrame()
        self.toolbar_card.setObjectName("card")
        tl = QHBoxLayout(self.toolbar_card)
        tl.setContentsMargins(8, 4, 8, 4)
        self.tab_toolbars = QStackedWidget()
        self.tab_toolbars.addWidget(QWidget())  # empty slot
        tl.addWidget(self.tab_toolbars)
        self.toolbar_card.setVisible(False)

        # camera presets: a card the window places under the view (bottom left)
        self.camera_card = QFrame()
        self.camera_card.setObjectName("card")
        cl = QHBoxLayout(self.camera_card)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(2)
        self.camera_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self)
        for key, label in CAMERAS.items():
            b = QToolButton()
            b.setText(label)
            b.setToolTip(f"Vue {label.lower()}")
            b.setCheckable(True)
            b.setProperty("kind", "segment")
            b.clicked.connect(lambda _=False, k=key: self.scene.set_camera(k))
            group.addButton(b)
            cl.addWidget(b)
            self.camera_buttons[key] = b
        self.camera_buttons["iso"].setChecked(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.toolbar_card)
        layout.addWidget(self.scene, 1)

        #: the open tab's hooks (set by the window): drag (key, point) -> Drag | None,
        #: click (Pick) -> bool (True = the tab used the click)
        self.drag_handler: Callable[[str, tuple], Drag | None] | None = None
        self.click_handler: Callable[[Pick], bool] | None = None
        self.drag: Drag | None = None  # the drag in progress
        self.scene.clicked.connect(self._on_clicked)
        self.scene.drag_start = self.drag_start
        self.scene.drag_move = self.drag_move
        self.scene.drag_end = self.drag_end
        selection.changed.connect(lambda _keys: self._redraw())

    def set_tab_toolbar(self, widget: QWidget | None) -> None:
        self.toolbar_card.setVisible(widget is not None)
        if widget is None:
            self.tab_toolbars.setCurrentIndex(0)
            return
        if self.tab_toolbars.indexOf(widget) < 0:
            self.tab_toolbars.addWidget(widget)
        self.tab_toolbars.setCurrentWidget(widget)

    def show_items(self, items: list[SceneItem], reset_camera: bool = False) -> None:
        self.items = list(items)
        self._redraw(reset_camera)

    def bounds_of(self, keys) -> tuple[float, ...] | None:
        """Union of the bounds of the drawn items with these selection keys."""
        wanted = set(keys)
        boxes = [it.mesh.bounds for it in self.items if it.key in wanted]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), max(b[1] for b in boxes),
                min(b[2] for b in boxes), max(b[3] for b in boxes),
                min(b[4] for b in boxes), max(b[5] for b in boxes))

    def frame(self, keys=None) -> tuple[float, ...] | None:
        """Point the camera at the objects ``keys`` (default: the selection).
        Returns the framed bounds, or None when none of them is drawn."""
        bounds = self.bounds_of(self.selection.keys if keys is None else keys)
        if bounds is not None:
            self.scene.frame_bounds(bounds)
        return bounds

    def _redraw(self, reset_camera: bool = False) -> None:
        self.scene.draw(highlight_items(self.items, self.selection.keys), reset_camera)
        self.redrawn.emit()

    def _on_clicked(self, pick: Pick) -> None:
        if self.click_handler is not None and self.click_handler(pick):
            return
        if pick.double and pick.key is not None:
            self.frame([pick.key])
            return
        self.selection.select(pick.key, pick.additive)

    # ---- dragging objects with the mouse (the scene view calls these; so do tests)
    def drag_start(self, key: str, point: tuple) -> bool:
        """A press on the item ``key`` at ``point``: True if the open tab drags it."""
        self.drag = self.drag_handler(key, tuple(point)) if self.drag_handler else None
        self.scene.drag_keys = list(self.drag.keys) if self.drag else []
        return self.drag is not None

    def drag_move(self, delta: tuple, free: bool = False) -> tuple:
        """The mouse moved ``delta`` from the grabbed point; returns the move shown."""
        if self.drag is None:
            return tuple(delta)
        if self.drag.constrain is None:
            return tuple(delta)
        return tuple(self.drag.constrain(tuple(delta), free))

    def drag_end(self, delta: tuple, free: bool = False) -> bool:
        """Mouse released: the tab turns the move into one edit. True if the model
        changed."""
        drag, self.drag = self.drag, None
        if drag is None:
            return False
        if all(abs(v) < 1e-12 for v in delta):
            if drag.cancel is not None:
                drag.cancel()
            return False
        return bool(drag.finish(tuple(delta), free))

    def close_plotter(self) -> None:
        self.scene.close_plotter()
