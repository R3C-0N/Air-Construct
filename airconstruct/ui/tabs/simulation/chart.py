"""Convergence chart: residual of every field (worst region) against the iteration, on a
log scale, with the phase boundaries. pyqtgraph, redrawn at most a few times a second.

Owned by: thread "Simulation".
"""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...theme import COLORS

#: fields drawn, in legend order; the others (k, omega, epsilon...) only in expert mode
MAIN_FIELDS = ("Ux", "Uy", "Uz", "p_rgh", "h")
#: categorical colours (blue, orange, teal, purple, red, then greys)
PALETTE = ("#2563EB", "#EA580C", "#0D9488", "#7C3AED", "#DB2777", "#65A30D", "#6B7280",
           "#A16207")
PHASE_LABELS = {"flow": "Écoulement", "energy": "Températures", "check": "Flottabilité"}


def residual_lines(history: list, fields: tuple[str, ...] | None = None
                   ) -> dict[str, tuple[list[int], list[float]]]:
    """{field: (iterations, residual)}: the worst residual over the regions of each
    field, from monitor.Progress snapshots. ``fields`` keeps only those (in that order)."""
    out: dict[str, tuple[list[int], list[float]]] = {}
    for p in history:
        worst: dict[str, float] = {}
        for regs in p.residuals.values():
            for f, r in regs.items():
                if r is not None and math.isfinite(r) and r > 0:
                    worst[f] = max(worst.get(f, 0.0), r)
        for f, r in worst.items():
            its, vals = out.setdefault(f, ([], []))
            its.append(p.iteration)
            vals.append(r)
    if fields is not None:
        out = {f: out[f] for f in fields if f in out}
    return out


def phase_starts(history: list) -> list[tuple[int, str]]:
    """[(iteration, phase)] where each solver phase began (for the chart's markers)."""
    out: list[tuple[int, str]] = []
    for p in history:
        if not out or out[-1][1] != p.phase:
            out.append((p.iteration, p.phase))
    return out


class ConvergenceChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background=COLORS["card"])
        self.plot.setMinimumHeight(170)
        self.plot.setLogMode(x=False, y=True)
        self.plot.showGrid(x=False, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "itération")
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        for ax in ("left", "bottom"):
            self.plot.getAxis(ax).setTextPen(COLORS["muted"])
            self.plot.getAxis(ax).setPen(COLORS["input_border"])
        self.legend = self.plot.addLegend(offset=(-6, 4), labelTextColor=COLORS["text_secondary"],
                                          colCount=3, verSpacing=-6)
        self.curves: dict[str, pg.PlotDataItem] = {}
        self._markers: list = []
        self.show_all_fields = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)

    def set_history(self, history: list) -> None:
        fields = None if self.show_all_fields else MAIN_FIELDS
        lines = residual_lines(history, fields)
        for name in list(self.curves):
            if name not in lines:
                self.plot.removeItem(self.curves.pop(name))
                self.legend.removeItem(name)
        for name, (its, vals) in lines.items():
            curve = self.curves.get(name)
            if curve is None:
                color = PALETTE[len(self.curves) % len(PALETTE)]
                curve = self.plot.plot([], [], name=name, pen=pg.mkPen(color, width=2))
                self.curves[name] = curve
            curve.setData(its, vals)
        for m in self._markers:
            self.plot.removeItem(m)
        self._markers = []
        for it, phase in phase_starts(history)[1:]:
            line = pg.InfiniteLine(it, angle=90, pen=pg.mkPen(COLORS["input_border"],
                                                              style=pg.QtCore.Qt.PenStyle.DashLine),
                                   label=PHASE_LABELS.get(phase, phase),
                                   labelOpts={"position": 0.06, "color": COLORS["muted"]})
            self.plot.addItem(line)
            self._markers.append(line)

    def clear(self) -> None:
        self.set_history([])

    def dispose(self) -> None:
        """Empty the pyqtgraph scene while every Python item is still alive: with
        PySide6, a scene destroyed later from C++ with Python-made items in it can
        crash. Call it when the window closes (idempotent)."""
        if getattr(self, "_disposed", False):
            return
        self._disposed = True
        scene = self.plot.scene()
        self.curves.clear()
        self._markers = []
        if scene is not None:
            scene.clear()
