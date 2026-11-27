"""Fan operating point chart: the fan curve against the enclosure's resistance.

Owned by: thread "Air". Uses the instant 1D estimate (``airconstruct.fans``): the
system curve is the pressure the enclosure needs for a total flow; the operating point
is where the fans' curves meet it. The CFD result replaces it after the run.

Drawn with QPainter rather than pyqtgraph: a pyqtgraph PlotWidget nested in the
tab's panels crashed (segfault in QGraphicsScene's destructor) when the window was
destroyed, and this chart only needs two lines and a dot.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from ....fans import estimate_flow, system_loss
from ...theme import COLORS, set_prop
from .ops import M3H


def chart_series(case, name: str) -> dict:
    """Points to draw for opening ``name`` (flows in m3/h, pressures in Pa):
    ``fan`` (curve or None), ``system``, ``point`` (q, dp) or None, ``fixed`` flag."""
    o = next((p for p in case.openings if p.name == name), None)
    if o is None or o.fan is None:
        return {"fan": None, "system": [], "point": None, "fixed": False}
    est = estimate_flow(case)
    fp = next((f for f in est.fans if f.name == name), None)
    q_top = max(o.fan.curve[-1][0] if o.fan.curve else (o.fan.flow_rate or 0.0) * 1.6,
                est.flow_rate * 1.3, 1e-5)
    n = 40
    system = [(q_top * i / n * M3H, system_loss(case, q_top * i / n)) for i in range(n + 1)]
    curve = [(q * M3H, dp) for q, dp in o.fan.curve] if o.fan.curve else None
    point = (fp.flow_rate * M3H, fp.pressure) if fp else None
    return {"fan": curve, "system": system, "point": point, "fixed": o.fan.curve is None,
            "total": est.flow_rate * M3H}


def nice_step(span: float, ticks: int = 4) -> float:
    """A round tick spacing (1, 2, 5 x 10^n) giving about ``ticks`` ticks over ``span``."""
    if span <= 0:
        return 1.0
    raw = span / ticks
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)


def _fmt(v: float) -> str:
    return f"{v:g}".replace(".", ",")


class Plot(QWidget):
    """Axes, the system curve (dashed), the fan curve (blue) and the operating point."""

    LEFT, BOTTOM, TOP, RIGHT = 38, 30, 14, 12

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.series: dict = {}
        self.x_max = 1.0
        self.y_max = 1.0
        self.setMinimumSize(160, 190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(300, 200)

    def set_series(self, s: dict) -> None:
        self.series = s
        xs = [p[0] for p in s.get("fan") or []] + [s["system"][-1][0] if s.get("system") else 1]
        ys = [p[1] for p in s.get("fan") or []] + [s["point"][1] if s.get("point") else 0, 1]
        self.x_max = max(xs) or 1.0
        self.y_max = max(ys) * 1.25
        self.update()

    def _map(self, x: float, y: float) -> QPointF:
        w = self.width() - self.LEFT - self.RIGHT
        h = self.height() - self.TOP - self.BOTTOM
        return QPointF(self.LEFT + w * x / self.x_max,
                       self.TOP + h * (1 - min(y, self.y_max * 1.05) / self.y_max))

    def _path(self, pts) -> QPainterPath:
        path = QPainterPath()
        for i, (x, y) in enumerate(pts):
            pt = self._map(x, y)
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        return path

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.LEFT, self.TOP, self.width() - self.LEFT - self.RIGHT,
                      self.height() - self.TOP - self.BOTTOM)
        font = QFont(self.font())
        font.setPixelSize(10)
        p.setFont(font)
        grid = QPen(QColor(COLORS["divider"]), 1)
        text = QColor(COLORS["muted"])
        for axis, top in (("x", self.x_max), ("y", self.y_max)):
            step = nice_step(top)
            v = 0.0
            while v <= top + 1e-9:
                if axis == "x":
                    a, b = self._map(v, 0), self._map(v, self.y_max)
                    p.setPen(grid)
                    p.drawLine(a, b)
                    p.setPen(text)
                    p.drawText(QRectF(a.x() - 20, area.bottom() + 2, 40, 12),
                               Qt.AlignmentFlag.AlignHCenter, _fmt(round(v, 6)))
                else:
                    a, b = self._map(0, v), self._map(self.x_max, v)
                    p.setPen(grid)
                    p.drawLine(a, b)
                    p.setPen(text)
                    p.drawText(QRectF(0, a.y() - 6, self.LEFT - 4, 12),
                               Qt.AlignmentFlag.AlignRight, _fmt(round(v, 6)))
                v += step
        p.setPen(text)
        p.drawText(QRectF(area.left(), area.bottom() + 15, area.width(), 14),
                   Qt.AlignmentFlag.AlignHCenter, "Débit (m³/h)")
        p.drawText(QRectF(2, 0, 80, 12), Qt.AlignmentFlag.AlignLeft, "Pa")
        p.setPen(QPen(QColor(COLORS["input_border"]), 1))
        p.drawLine(area.bottomLeft(), area.bottomRight())
        p.drawLine(area.bottomLeft(), area.topLeft())

        s = self.series
        p.setClipRect(area.adjusted(-6, -6, 6, 6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if s.get("system"):
            p.setPen(QPen(QColor(COLORS["subtle"]), 2, Qt.PenStyle.DashLine))
            p.drawPath(self._path(s["system"]))
        accent = QColor(COLORS["accent"])
        if s.get("fan"):
            p.setPen(QPen(accent, 2.5))
            p.drawPath(self._path(s["fan"]))
            p.setBrush(accent)
            for x, y in s["fan"]:
                p.drawEllipse(self._map(x, y), 2.5, 2.5)
        if s.get("point"):
            q, dp = s["point"]
            if s.get("fixed"):
                p.setPen(QPen(accent, 2))
                p.drawLine(self._map(q, 0), self._map(q, self.y_max))
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor(COLORS["heat"]))
            p.drawEllipse(self._map(q, dp), 6, 6)
        p.end()


class FanChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.plot = Plot()
        lay.addWidget(self.plot)
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        set_prop(self.caption, role="muted")
        lay.addWidget(self.caption)
        self.series: dict = {}

    def show_case(self, case, name: str) -> None:
        if case is None:
            self.series = {}
            self.plot.set_series({})
            self.caption.setText("Le cas n’est pas valide : point de fonctionnement indisponible.")
            return
        try:
            s = chart_series(case, name)
        except Exception as exc:  # noqa: BLE001 - never break the inspector
            self.series = {}
            self.plot.set_series({})
            self.caption.setText(f"Point de fonctionnement indisponible : {exc}")
            return
        self.series = s
        self.plot.set_series(s)
        if s["point"]:
            q, dp = s["point"]
            qs = f"{q:.1f}".replace(".", ",")
            dps = f"{dp:.1f}".replace(".", ",")
            what = "débit imposé" if s["fixed"] else "estimation instantanée"
            self.caption.setText(f"Point de fonctionnement (orange) : {qs} m³/h sous {dps} Pa "
                                 f"({what}). En bleu le ventilateur, en tirets la résistance du "
                                 "boîtier. Le calcul donnera la valeur réelle.")
        else:
            self.caption.setText("")
