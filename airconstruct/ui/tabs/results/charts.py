"""2D charts of the Résultats tab: temperatures per part against their limit, values
along the A→B line, and the energy balance as a Sankey diagram.

Owned by: thread "Résultats" (see docs/ui-architecture.md).
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...theme import COLORS
from .data import PartRow, finite, fr


class _Chart(QWidget):
    """Small painted chart on the card (plain QPainter: light, and nothing left behind
    when the window closes)."""

    MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 70.0, 14.0, 8.0, 30.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def _font(self, painter: QPainter, px: int = 11) -> None:
        f = QFont(self.font())
        f.setPixelSize(px)
        painter.setFont(f)

    def _plot_rect(self) -> QRectF:
        return QRectF(self.MARGIN_L, self.MARGIN_T,
                      max(10.0, self.width() - self.MARGIN_L - self.MARGIN_R),
                      max(10.0, self.height() - self.MARGIN_T - self.MARGIN_B))

    def _x_axis(self, p: QPainter, r: QRectF, lo: float, hi: float, label: str) -> None:
        p.setPen(QPen(QColor(COLORS["divider"])))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
        p.setPen(QPen(QColor(COLORS["muted"])))
        for v in nice_ticks(lo, hi):
            x = r.left() + (v - lo) / (hi - lo) * r.width()
            p.drawLine(QPointF(x, r.bottom()), QPointF(x, r.bottom() + 3))
            p.drawText(QRectF(x - 30, r.bottom() + 3, 60, 14),
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                       fr(v, 0 if hi - lo >= 5 else 1))
        p.drawText(QRectF(r.left(), r.bottom() + 15, r.width(), 14),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), label)


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """About ``n`` round values between lo and hi."""
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return []
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step) * step
    return [first + i * step for i in range(int((hi - first) / step) + 1)]


class PartChart(_Chart):
    """Horizontal bars min → max per part, a dot at the mean, a tick at the limit."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[PartRow] = []
        self.unit = "°C"

    def set_rows(self, rows: list[PartRow], unit: str = "°C") -> None:
        self.rows = [r for r in rows if finite(r.t_max)]
        self.unit = unit
        self.setMinimumHeight(int(self.MARGIN_T + self.MARGIN_B + 26 * max(1, len(self.rows))))
        self.update()

    @staticmethod
    def _low(r: PartRow) -> float | None:
        """Start of the bar: the minimum, else None (bar from the axis, estimate)."""
        return r.t_min if finite(r.t_min) else None

    def x_range(self) -> tuple[float, float]:
        lows = [self._low(r) for r in self.rows]
        xs = [v for v in lows if v is not None] + [r.t_max for r in self.rows]
        xs += [r.limit for r in self.rows if r.limit is not None]
        lo, hi = min(xs), max(xs)
        if any(v is None for v in lows):  # plain bars: start from a round value below
            lo = min(lo, 0.0 if lo > 0 else lo) if lo < 50 else lo - 0.6 * (hi - lo or lo)
        span = hi - lo or 1.0
        return lo - 0.05 * span, hi + 0.06 * span

    def paintEvent(self, _event) -> None:
        if not self.rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._font(p)
        r = self._plot_rect()
        lo, hi = self.x_range()

        def X(v):
            return r.left() + (v - lo) / (hi - lo) * r.width()

        band = r.height() / len(self.rows)
        for i, row in enumerate(self.rows):
            yc = r.top() + (i + 0.5) * band
            p.setPen(QPen(QColor(COLORS["text"])))
            p.drawText(QRectF(0, yc - 8, self.MARGIN_L - 8, 16),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), row.name)
            low = self._low(row)
            x0, x1 = (X(low) if low is not None else r.left()), X(row.t_max)
            color = QColor(COLORS["error"] if row.status == "ko" else COLORS["heat"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            h = min(12.0, band * 0.5)
            p.drawRoundedRect(QRectF(x0, yc - h / 2, max(3.0, x1 - x0), h), 3, 3)
            if finite(row.t_mean):
                p.setBrush(QBrush(QColor("white")))
                p.setPen(QPen(QColor(COLORS["text"]), 1))
                p.drawEllipse(QPointF(X(row.t_mean), yc), 3.5, 3.5)
            if row.limit is not None:
                lc = QColor(COLORS["error_text"] if row.status == "ko" else COLORS["text"])
                p.setPen(QPen(lc, 2.5))
                xl = X(row.limit)
                p.drawLine(QPointF(xl, yc - h), QPointF(xl, yc + h))
        self._x_axis(p, r, lo, hi, f"Température ({self.unit}) · trait = limite")
        p.end()


class LineChart(_Chart):
    """Values of the shown field along the measuring line A→B."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.distance: np.ndarray = np.zeros(0)
        self.values: np.ndarray = np.zeros(0)
        self.length_unit = ""
        self.field_label = ""

    def set_data(self, distance_display: np.ndarray, values: np.ndarray, length_unit: str,
                 field_label: str) -> None:
        self.distance = np.asarray(distance_display, float)
        self.values = np.asarray(values, float)
        self.length_unit, self.field_label = length_unit, field_label
        self.update()

    def clear(self) -> None:
        self.set_data(np.zeros(0), np.zeros(0), "", "")

    def paintEvent(self, _event) -> None:
        ok = np.isfinite(self.values)
        if len(self.distance) < 2 or not ok.any():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._font(p)
        r = self._plot_rect()
        x_lo, x_hi = float(self.distance[0]), float(self.distance[-1])
        if x_hi <= x_lo:
            p.end()
            return
        v = self.values[ok]
        y_lo, y_hi = float(v.min()), float(v.max())
        pad = 0.08 * (y_hi - y_lo or 1.0)
        y_lo, y_hi = y_lo - pad, y_hi + pad

        def pt(d, val):
            return QPointF(r.left() + (d - x_lo) / (x_hi - x_lo) * r.width(),
                           r.bottom() - (val - y_lo) / (y_hi - y_lo) * r.height())

        p.setPen(QPen(QColor(COLORS["muted"])))
        for t in nice_ticks(y_lo, y_hi, 4):
            y = pt(x_lo, t).y()
            p.setPen(QPen(QColor(COLORS["divider"])))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QPen(QColor(COLORS["muted"])))
            p.drawText(QRectF(0, y - 7, self.MARGIN_L - 6, 14),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                       fr(t, 0 if y_hi - y_lo >= 5 else 2))
        path, started = QPainterPath(), False
        for d, val in zip(self.distance, self.values, strict=True):
            if not math.isfinite(val):
                started = False
                continue
            q = pt(d, val)
            if started:
                path.lineTo(q)
            else:
                path.moveTo(q)
                started = True
        p.setPen(QPen(QColor(COLORS["error"]), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        i = int(np.nanargmax(np.where(ok, self.values, -np.inf)))
        top = pt(self.distance[i], self.values[i])
        p.setPen(QPen(QColor(COLORS["error_text"])))
        p.drawText(QRectF(top.x() - 40, top.y() - 16, 80, 14),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
                   fr(self.values[i], 1))
        self._x_axis(p, r, x_lo, x_hi, f"Distance depuis A ({self.length_unit})")
        p.save()
        p.setPen(QPen(QColor(COLORS["muted"])))
        p.translate(10, r.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-r.height() / 2, -6, r.height(), 14),
                   int(Qt.AlignmentFlag.AlignCenter), self.field_label)
        p.restore()
        p.end()


# --------------------------------------------------------------------------- Sankey

KIND_COLORS = {"part": "heat", "air": "accent", "walls": "subtle", "opening": "accent_dark",
               "residual": "warning"}
KIND_LABELS = {"air": "air", "walls": "parois", "residual": "écart"}


def sankey_layout(sankey: dict) -> tuple[dict[str, int], dict[str, float]]:
    """Column of each node (longest path from the sources) and its size [W]."""
    nodes = [n["id"] for n in sankey.get("nodes", [])]
    links = sankey.get("links", [])
    col = dict.fromkeys(nodes, 0)
    for _ in range(len(nodes)):  # longest path; a cycle stops after len(nodes) passes
        changed = False
        for link in links:
            s, t = link["source"], link["target"]
            if s in col and t in col and col[t] < col[s] + 1 <= len(nodes):
                col[t] = col[s] + 1
                changed = True
        if not changed:
            break
    sinks = {"walls", "residual"} | {n["id"] for n in sankey.get("nodes", [])
                                     if n.get("kind") == "opening"}
    last = max(col.values(), default=0)
    for n in nodes:  # what leaves the box goes to the last column
        if n in sinks and not any(link["source"] == n for link in links):
            col[n] = last
    size = {}
    for n in nodes:
        out = sum(link["value"] for link in links if link["source"] == n)
        inn = sum(link["value"] for link in links if link["target"] == n)
        size[n] = max(out, inn)
    return col, size


class SankeyWidget(QWidget):
    """Energy balance: parts → air / walls → openings, band width ∝ watts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.sankey: dict = {"nodes": [], "links": []}
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_sankey(self, sankey: dict | None) -> None:
        self.sankey = sankey or {"nodes": [], "links": []}
        self.update()

    def _label(self, node: dict, watts: float) -> str:
        name = KIND_LABELS.get(node["id"], node.get("label", node["id"]))
        return f"{name} {fr(watts, 1)} W"

    def paintEvent(self, _event) -> None:
        links = [lk for lk in self.sankey.get("links", []) if lk["value"] > 0]
        nodes = {n["id"]: n for n in self.sankey.get("nodes", [])}
        if not links or not nodes:
            return
        col, size = sankey_layout(self.sankey)
        ncol = max(col.values()) + 1
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        W, H = self.width(), self.height()
        node_w, gap, label_w = 8.0, 8.0, 92.0
        x_of = {c: label_w + (W - 2 * label_w - node_w) * (c / max(1, ncol - 1))
                for c in range(ncol)}
        columns: dict[int, list[str]] = {}
        for n in nodes:
            columns.setdefault(col[n], []).append(n)
        scale = min((H - 8 - gap * (len(ids) - 1)) / max(1e-9, sum(size[i] for i in ids))
                    for ids in columns.values())
        rect: dict[str, QRectF] = {}
        for c, ids in columns.items():
            ids.sort(key=lambda i: -size[i])
            total = sum(size[i] for i in ids) * scale + gap * (len(ids) - 1)
            y = (H - total) / 2
            for i in ids:
                h = max(2.0, size[i] * scale)
                rect[i] = QRectF(x_of[c], y, node_w, h)
                y += h + gap
        out_off = dict.fromkeys(nodes, 0.0)
        in_off = dict.fromkeys(nodes, 0.0)
        for lk in sorted(links, key=lambda lk: (rect[lk["source"]].y(), rect[lk["target"]].y())):
            s, t = rect[lk["source"]], rect[lk["target"]]
            h = lk["value"] * scale
            y0 = s.y() + out_off[lk["source"]]
            y1 = t.y() + in_off[lk["target"]]
            out_off[lk["source"]] += h
            in_off[lk["target"]] += h
            x0, x1 = s.right(), t.left()
            if x1 <= x0:  # same column (rare): draw a short loop on the right
                x1 = x0 + 30
            mx = (x0 + x1) / 2
            path = QPainterPath(QPointF(x0, y0))
            path.cubicTo(QPointF(mx, y0), QPointF(mx, y1), QPointF(x1, y1))
            path.lineTo(QPointF(x1, y1 + h))
            path.cubicTo(QPointF(mx, y1 + h), QPointF(mx, y0 + h), QPointF(x0, y0 + h))
            path.closeSubpath()
            color = QColor(COLORS[KIND_COLORS.get(nodes[lk["source"]].get("kind"), "subtle")])
            if lk.get("kind") == "residual":
                color = QColor(COLORS["warning"])
            color.setAlpha(70)
            p.fillPath(path, QBrush(color))
        for i, r in rect.items():
            p.fillRect(r, QColor(COLORS[KIND_COLORS.get(nodes[i].get("kind"), "subtle")]))
            p.setPen(QPen(QColor(COLORS["text_secondary"])))
            text = self._label(nodes[i], size[i])
            if col[i] == 0:
                box = QRectF(r.left() - label_w - 4, r.center().y() - 8, label_w, 16)
                p.drawText(box, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                           text)
            else:
                box = QRectF(r.right() + 4, r.center().y() - 8, label_w, 16)
                p.drawText(box, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                           text)
        p.end()


def simple_sankey(summary) -> dict | None:
    """Sankey data from the summary alone (when no detailed energy balance exists)."""
    power = getattr(summary, "total_power_w", 0.0) or sum(
        c.power_w for c in summary.components if finite(c.power_w))
    if not power or power <= 0:
        return None
    to_air = getattr(summary, "heat_to_air_w", math.nan)
    walls = getattr(summary, "heat_through_walls_w", 0.0) or 0.0
    nodes = [{"id": c.name, "label": c.name, "kind": "part", "power_w": c.power_w}
             for c in summary.components if finite(c.power_w) and c.power_w > 0]
    links = [{"source": n["id"], "target": "air", "value": n["power_w"], "kind": "convection"}
             for n in nodes]
    nodes.append({"id": "air", "label": "air", "kind": "air", "power_w": 0.0})
    if finite(to_air) and to_air > 0:
        nodes.append({"id": "sortie", "label": "sortie", "kind": "opening", "power_w": 0.0})
        links.append({"source": "air", "target": "sortie", "value": to_air, "kind": "opening"})
    if walls > 0:
        nodes.append({"id": "walls", "label": "walls", "kind": "walls", "power_w": 0.0})
        links.append({"source": "air", "target": "walls", "value": walls, "kind": "wall"})
    return {"nodes": nodes, "links": links}
