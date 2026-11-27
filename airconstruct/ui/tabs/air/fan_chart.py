"""Fan operating point chart: the fan curve against the enclosure's resistance.

Owned by: thread "Air". Uses the instant 1D estimate (``airconstruct.fans``): the
system curve is the pressure the enclosure needs for a total flow; the operating point
is where the fans' curves meet it. The CFD result replaces it after the run.
"""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ....fans import estimate_flow, system_loss
from ...theme import COLORS, set_prop
from .ops import M3H

pg.setConfigOptions(antialias=True)


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


class FanChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.plot = pg.PlotWidget(background=COLORS["card"])
        self.plot.setMinimumHeight(190)
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()
        pi = self.plot.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.15)
        for ax in ("left", "bottom"):
            a = pi.getAxis(ax)
            a.setTextPen(COLORS["muted"])
            a.setPen(COLORS["input_border"])
        pi.setLabel("bottom", "Débit (m³/h)")
        pi.setLabel("left", "Pression (Pa)")
        self.legend = pi.addLegend(offset=(-6, 6), labelTextColor=COLORS["text_secondary"])
        lay.addWidget(self.plot)
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        set_prop(self.caption, role="muted")
        lay.addWidget(self.caption)
        self.series: dict = {}

    def show_case(self, case, name: str) -> None:
        pi = self.plot.getPlotItem()
        pi.clear()
        self.legend.clear()
        if case is None:
            self.series = {}
            self.caption.setText("Le cas n’est pas valide : point de fonctionnement indisponible.")
            return
        try:
            s = chart_series(case, name)
        except Exception as exc:  # noqa: BLE001 - never break the inspector
            self.series = {}
            self.caption.setText(f"Point de fonctionnement indisponible : {exc}")
            return
        self.series = s
        if s["system"]:
            xs, ys = zip(*s["system"])
            pi.plot(xs, ys, pen=pg.mkPen(COLORS["subtle"], width=2, style=pg.QtCore.Qt.PenStyle.DashLine),
                    name="Boîtier (résistance)")
        if s["fan"]:
            xs, ys = zip(*s["fan"])
            pi.plot(xs, ys, pen=pg.mkPen(COLORS["accent"], width=2.5), symbol="o", symbolSize=5,
                    symbolBrush=COLORS["accent"], symbolPen=None, name="Ventilateur")
        if s["point"]:
            q, dp = s["point"]
            if s["fixed"]:
                pi.addLine(x=q, pen=pg.mkPen(COLORS["accent"], width=2))
            pi.plot([q], [dp], pen=None, symbol="o", symbolSize=11, symbolBrush=COLORS["heat"],
                    symbolPen=pg.mkPen("white", width=2), name="Point de fonctionnement")
            qs = f"{q:.1f}".replace(".", ",")
            dps = f"{dp:.1f}".replace(".", ",")
            what = "débit imposé" if s["fixed"] else "estimation instantanée"
            self.caption.setText(f"Point de fonctionnement : {qs} m³/h sous {dps} Pa ({what}, "
                                 "le calcul donnera la valeur réelle).")
        else:
            self.caption.setText("")
