"""Move / resize handles on the selected parts, in the shared 3D view.

Owned by: thread "Modèle" (see docs/ui-architecture.md).

A VTK box widget (``plotter.add_box_widget``, no rotation) is put around the
selection; it is made again after each redraw (clearing the scene removes its
actors). Dragging its centre moves, dragging a face handle resizes. When the
mouse is released the tab gets the new bounds (:attr:`on_bounds`) and turns them
into one project edit, snapped to the grid unless Alt is held. The widget is put
back after every redraw of the viewport (``Viewport.redrawn``), so it always shows
the data. Without a 3D view (tests) nothing is drawn; the tab logic is the same.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ...theme import COLORS


class BoxHandles:
    def __init__(self, viewport, on_bounds: Callable[[tuple, bool], None]) -> None:
        self.viewport = viewport
        self.on_bounds = on_bounds
        self.bounds: tuple | None = None
        self.widget = None
        self._placing = False

    @property
    def plotter(self):
        return getattr(self.viewport.scene, "plotter", None)

    def show(self, bounds: tuple | None) -> None:
        """Put the handles around ``bounds`` (xmin, xmax, ymin, ymax, zmin, zmax), or
        remove them (None)."""
        self.bounds = tuple(bounds) if bounds is not None else None
        p = self.plotter
        if p is None or self._placing:
            return
        self._placing = True  # pyvista calls the callback when the widget is created
        try:
            if self.bounds is None:
                self.clear()
                return
            self.clear()
            self.widget = p.add_box_widget(
                self._callback, bounds=self.bounds, factor=1.0, rotation_enabled=False,
                color=COLORS["accent"], use_planes=False, outline_translation=True,
                pass_widget=False, interaction_event="end")
            try:  # thinner, accent coloured handles
                self.widget.GetHandleProperty().SetColor(0.11, 0.31, 0.85)
                self.widget.GetSelectedHandleProperty().SetColor(0.98, 0.57, 0.24)
                self.widget.GetOutlineProperty().SetColor(0.11, 0.31, 0.85)
                self.widget.GetOutlineProperty().SetLineWidth(2)
                # VTK sizes handles from the placed box: a small part gets dots, so
                # scale them by how small the part is compared with the scene
                self.widget.SetHandleSize(self._handle_size())
                self.widget.PlaceWidget(self.bounds)  # sizes the handles again
            except Exception:  # noqa: BLE001, S110 - cosmetic only
                pass
        except Exception:  # noqa: BLE001 - handles are a convenience, never break the view
            self.widget = None
        finally:
            self._placing = False

    def _handle_size(self) -> float:
        b = self.bounds
        diag = sum((b[2 * i + 1] - b[2 * i]) ** 2 for i in range(3)) ** 0.5
        try:
            sb = self.plotter.renderer.ComputeVisiblePropBounds()
            scene = sum((sb[2 * i + 1] - sb[2 * i]) ** 2 for i in range(3)) ** 0.5
        except Exception:  # noqa: BLE001
            scene = diag
        return min(0.05, max(0.008, 0.007 * scene / max(diag, 1e-9)))

    def clear(self) -> None:
        p = self.plotter
        self.widget = None
        if p is not None:
            try:
                p.clear_box_widgets()
            except Exception:  # noqa: BLE001, S110
                pass

    def _callback(self, box) -> None:
        if self._placing or self.bounds is None:
            return
        new = tuple(float(v) for v in box.bounds)
        if all(abs(a - b) < 1e-9 for a, b in zip(new, self.bounds)):
            return
        free = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier)
        self.on_bounds(new, free)
