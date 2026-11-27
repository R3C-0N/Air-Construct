"""2D editor of one enclosure face: drag the openings, resize them by their corners,
read their distances to the edges.

Owned by: thread "Air". The widget only draws and reports gestures through signals;
the tab turns them into project edits (one undo step per drag).

Snapping: while dragging, an opening snaps to the face edges, the face centre lines
and the edges and centre lines of the other openings on the face (within 8 px).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...theme import COLORS
from .geometry import (
    Rect,
    clamp_center,
    component_footprints,
    face_axis_names,
    face_size,
    opening_rect,
    openings_on,
    snap_center,
    snap_value,
)

INLET_COLOR = "#2f80ed"
OUTLET_COLOR = "#27ae60"
MARGIN = 30  # px around the face, for the dimension labels
HANDLE = 7  # px, half size of a corner handle
SNAP_PX = 8
MIN_SIZE = 0.002  # m


def opening_color(o: dict) -> str:
    return INLET_COLOR if o.get("kind") == "inlet" else OUTLET_COLOR


class FaceEditor(QWidget):
    #: an opening was clicked (name) or the empty face (None)
    picked = Signal(object)
    #: drag in progress: name, (cu, cv), (su, sv) in metres
    geometry_edited = Signal(str, tuple, tuple)
    #: empty face clicked while a creation tool is armed: (u, v) in metres
    create_requested = Signal(tuple)

    def __init__(self, units, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.units = units
        self.data: dict = {}
        self.face = "xmin"
        self.selected: str | None = None
        self.armed: str | None = None  # "inlet", "outlet", "fan" or None
        self._drag: dict | None = None
        self._snap: tuple[str | None, str | None] = (None, None)
        self.setMinimumSize(240, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        units.changed.connect(self.update)

    def sizeHint(self) -> QSize:
        return QSize(320, 240)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        w, h = face_size(self.data, self.face) if self.data else (1.0, 1.0)
        if w <= 0 or h <= 0:
            return 220
        inner = max(width - 2 * MARGIN, 50)
        return int(min(max(inner * h / w + 2 * MARGIN, 160), 420))

    # ------------------------------------------------------------------ state
    def set_state(self, data: dict, face: str, selected: str | None) -> None:
        changed_face = face != self.face
        self.data = data
        self.face = face
        if self._drag is None or changed_face:
            self._drag = None
            self.selected = selected
        self.updateGeometry()
        self.update()

    def set_armed(self, tool: str | None) -> None:
        self.armed = tool
        self.setCursor(Qt.CursorShape.CrossCursor if tool else Qt.CursorShape.ArrowCursor)
        self.update()

    # ------------------------------------------------------------------ mapping
    def _frame(self) -> tuple[float, float, float, float, float]:
        """(left, top, scale px/m, face width m, face height m)."""
        w, h = face_size(self.data, self.face)
        if w <= 0 or h <= 0:
            return (MARGIN, MARGIN, 1.0, 1.0, 1.0)
        avail_w = max(self.width() - 2 * MARGIN, 10)
        avail_h = max(self.height() - 2 * MARGIN, 10)
        s = min(avail_w / w, avail_h / h)
        left = (self.width() - w * s) / 2
        top = (self.height() - h * s) / 2
        return (left, top, s, w, h)

    def to_screen(self, u: float, v: float) -> QPointF:
        left, top, s, _w, h = self._frame()
        return QPointF(left + u * s, top + (h - v) * s)

    def to_face(self, p: QPointF) -> tuple[float, float]:
        left, top, s, _w, h = self._frame()
        return ((p.x() - left) / s, h - (p.y() - top) / s)

    def rect_to_screen(self, r: Rect) -> QRectF:
        a, b = self.to_screen(r.u0, r.v1), self.to_screen(r.u1, r.v0)
        return QRectF(a, b)

    def _openings(self) -> list[tuple[dict, Rect]]:
        out = []
        for o in openings_on(self.data, self.face):
            r = opening_rect(o)
            if r is not None:
                out.append((o, r))
        return out

    def _rect_of(self, name: str | None) -> Rect | None:
        for o, r in self._openings():
            if o.get("name") == name:
                return r
        return None

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top, s, w, h = self._frame()
        face = QRectF(left, top, w * s, h * s)
        p.setPen(QPen(QColor(COLORS["input_border"]), 1.5))
        p.setBrush(QColor(COLORS["field"]))
        p.drawRoundedRect(face, 4, 4)

        # centre lines
        pen = QPen(QColor(COLORS["card_border"]), 1, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(self.to_screen(w / 2, 0), self.to_screen(w / 2, h))
        p.drawLine(self.to_screen(0, h / 2), self.to_screen(w, h / 2))

        # parts touching the face: grey hatch (they block the opening)
        for name, r in component_footprints(self.data, self.face):
            rr = self.rect_to_screen(r).intersected(face)
            p.setPen(QPen(QColor("#9aa5b1"), 1))
            p.setBrush(QBrush(QColor("#9aa5b1"), Qt.BrushStyle.BDiagPattern))
            p.drawRect(rr)
            p.setPen(QColor(COLORS["subtle"]))
            p.drawText(rr.adjusted(3, 2, -2, -2), Qt.AlignmentFlag.AlignLeft, name)

        small = QFont(self.font())
        small.setPixelSize(11)
        p.setFont(small)
        for o, r in self._openings():
            self._paint_opening(p, o, r, o.get("name") == self.selected)

        sel = self._rect_of(self.selected)
        if sel is not None:
            self._paint_dimensions(p, sel, w, h)
            self._paint_snap(p, sel, w, h)

        # axis names
        p.setPen(QColor(COLORS["subtle"]))
        au, av = face_axis_names(self.face)
        p.drawText(QRectF(face.right() - 30, face.bottom() + 2, 40, 16),
                   Qt.AlignmentFlag.AlignRight, f"{au} →")
        p.drawText(QRectF(face.left() - MARGIN + 2, face.top() - 18, 40, 16),
                   Qt.AlignmentFlag.AlignLeft, f"↑ {av}")
        if self.armed:
            p.setPen(QColor(COLORS["accent"]))
            p.drawText(QRectF(face.left(), face.top() - 18, face.width(), 16),
                       Qt.AlignmentFlag.AlignHCenter, "Cliquez pour placer l’ouverture")
        p.end()

    def _paint_opening(self, p: QPainter, o: dict, r: Rect, selected: bool) -> None:
        rr = self.rect_to_screen(r)
        color = QColor(opening_color(o))
        fill = QColor(color)
        fill.setAlpha(70)
        p.setPen(QPen(QColor(COLORS["accent"]) if selected else color, 2.5 if selected else 1.5))
        p.setBrush(fill)
        p.drawRect(rr)
        if o.get("grille"):
            p.setPen(QPen(color, 1))
            p.setBrush(QBrush(color, Qt.BrushStyle.CrossPattern))
            p.drawRect(rr.adjusted(2, 2, -2, -2))
        if o.get("fan"):
            d = min(rr.width(), rr.height()) * 0.86
            c = rr.center()
            p.setPen(QPen(color, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, d / 2, d / 2)
            hub = QPainterPath()
            hub.addEllipse(c, d / 8, d / 8)
            p.fillPath(hub, color)
        p.setPen(QColor(COLORS["text"]))
        p.drawText(rr.adjusted(4, 2, -2, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   str(o.get("name")))
        if selected:
            p.setPen(QPen(QColor(COLORS["accent"]), 1.5))
            p.setBrush(QColor("white"))
            for corner in (rr.topLeft(), rr.topRight(), rr.bottomLeft(), rr.bottomRight()):
                p.drawRect(QRectF(corner.x() - HANDLE / 2 - 1, corner.y() - HANDLE / 2 - 1,
                                  HANDLE + 2, HANDLE + 2))

    def _paint_dimensions(self, p: QPainter, r: Rect, w: float, h: float) -> None:
        """Distances to the four edges, drawn as dimension lines with their value."""
        pen = QPen(QColor(COLORS["accent"]), 1)
        p.setPen(pen)
        cu, cv = r.center
        u = self.units
        segs = [  # (from, to, value, label side)
            ((0.0, cv), (r.u0, cv), r.u0),
            ((r.u1, cv), (w, cv), w - r.u1),
            ((cu, 0.0), (cu, r.v0), r.v0),
            ((cu, r.v1), (cu, h), h - r.v1),
        ]
        for a, b, val in segs:
            if val <= 1e-9:
                continue
            pa, pb = self.to_screen(*a), self.to_screen(*b)
            p.drawLine(pa, pb)
            for q in (pa, pb):
                horizontal = abs(pa.y() - pb.y()) < 0.5
                if horizontal:
                    p.drawLine(QPointF(q.x(), q.y() - 3), QPointF(q.x(), q.y() + 3))
                else:
                    p.drawLine(QPointF(q.x() - 3, q.y()), QPointF(q.x() + 3, q.y()))
            mid = (pa + pb) / 2
            text = u.fmt_length(val, 0 if u.length == "mm" else 3).replace(f" {u.length}", "")
            box = QRectF(mid.x() - 24, mid.y() - 8, 48, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(COLORS["accent"]))
            p.drawRoundedRect(box.adjusted(6, 0, -6, 0), 7, 7)
            p.setPen(QColor("white"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
            p.setPen(pen)

    def _paint_snap(self, p: QPainter, r: Rect, w: float, h: float) -> None:
        if self._drag is None:
            return
        p.setPen(QPen(QColor(COLORS["heat"]), 1, Qt.PenStyle.DashLine))
        cu, cv = r.center
        if self._snap[0] == "center":
            p.drawLine(self.to_screen(cu, 0), self.to_screen(cu, h))
        if self._snap[1] == "center":
            p.drawLine(self.to_screen(0, cv), self.to_screen(w, cv))

    # ------------------------------------------------------------------ mouse
    def _hit(self, pos: QPointF) -> tuple[str | None, str | None]:
        """(opening name, corner) under the mouse; corner is 'tl', 'tr', 'bl', 'br'."""
        sel = self._rect_of(self.selected)
        if sel is not None:
            rr = self.rect_to_screen(sel)
            for key, c in (("tl", rr.topLeft()), ("tr", rr.topRight()),
                           ("bl", rr.bottomLeft()), ("br", rr.bottomRight())):
                if abs(pos.x() - c.x()) <= HANDLE and abs(pos.y() - c.y()) <= HANDLE:
                    return self.selected, key
        for o, r in reversed(self._openings()):
            if self.rect_to_screen(r).adjusted(-2, -2, 2, 2).contains(pos):
                return str(o.get("name")), None
        return None, None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        name, corner = self._hit(pos)
        if name is None:
            u, v = self.to_face(pos)
            _l, _t, _s, w, h = self._frame()
            if self.armed and 0 <= u <= w and 0 <= v <= h:
                self.create_requested.emit((u, v))
            else:
                self.selected = None
                self.picked.emit(None)
            self.update()
            return
        self.selected = name
        self.picked.emit(name)
        r = self._rect_of(name)
        self._drag = {"name": name, "corner": corner, "start": self.to_face(pos), "rect": r}
        self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self._drag is None:
            name, corner = self._hit(pos)
            if corner in ("tl", "br"):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif corner in ("tr", "bl"):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif name is not None:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor if self.armed
                               else Qt.CursorShape.ArrowCursor)
            return
        center, size = self.drag_to(self.to_face(pos))
        self.geometry_edited.emit(self._drag["name"], center, size)

    def drag_to(self, at: tuple[float, float]) -> tuple[tuple[float, float], tuple[float, float]]:
        """New (centre, size) of the dragged opening with the mouse at ``at`` (metres)."""
        d = self._drag
        r: Rect = d["rect"]
        _l, _t, s, w, h = self._frame()
        tol = SNAP_PX / s
        others = [rr for o, rr in self._openings() if o.get("name") != d["name"]]
        du, dv = at[0] - d["start"][0], at[1] - d["start"][1]
        if d["corner"] is None:
            c = (r.center[0] + du, r.center[1] + dv)
            c = clamp_center(c, r.size, w, h)
            c, self._snap = snap_center(c, r.size, w, h, tol, others)
            self._preview(d["name"], Rect.from_center(c, r.size))
            return (c, r.size)
        # resize: the opposite corner stays, the dragged one follows the mouse
        corner = d["corner"]
        u_fixed = r.u1 if "l" in corner else r.u0
        v_fixed = r.v0 if "t" in corner else r.v1
        u_targets = [0.0, w, w / 2] + [x for o in others for x in (o.u0, o.u1)]
        v_targets = [0.0, h, h / 2] + [y for o in others for y in (o.v0, o.v1)]
        u_new = snap_value(min(max(at[0], 0.0), w), u_targets, tol)
        v_new = snap_value(min(max(at[1], 0.0), h), v_targets, tol)
        if abs(u_new - u_fixed) < MIN_SIZE:
            u_new = u_fixed + (MIN_SIZE if "r" in corner else -MIN_SIZE)
        if abs(v_new - v_fixed) < MIN_SIZE:
            v_new = v_fixed + (MIN_SIZE if "t" in corner else -MIN_SIZE)
        nr = Rect(min(u_new, u_fixed), min(v_new, v_fixed), max(u_new, u_fixed),
                  max(v_new, v_fixed))
        self._snap = (None, None)
        self._preview(d["name"], nr)
        return (nr.center, nr.size)

    def _preview(self, name: str, r: Rect) -> None:
        for o in self.data.get("openings") or []:
            if o.get("name") == name:
                o["center"] = [r.center[0], r.center[1]]
                o["size"] = [r.size[0], r.size[1]]
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        self._drag = None
        self._snap = (None, None)
        self.update()
