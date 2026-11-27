"""Widgets of the Résultats side panel: empty state, stale banner, key numbers,
per-part table with limits, comparison of two runs.

Owned by: thread "Résultats" (see docs/ui-architecture.md).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...theme import COLORS, set_prop
from .data import NA, CompareRow, PartRow, finite, fr

NO_RESULTS = "Pas encore de résultats. Lancez le calcul dans l’étape Simulation."
RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _table(columns: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(columns))
    t.setHorizontalHeaderLabels(columns)
    t.verticalHeader().setVisible(False)
    h = t.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    h.setMinimumSectionSize(36)
    t.setShowGrid(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.setWordWrap(False)
    return t


def _fit_height(t: QTableWidget) -> None:
    h = t.horizontalHeader().height() + 4
    for r in range(t.rowCount()):
        h += t.rowHeight(r)
    t.setFixedHeight(h)


class ResultsPanel(QWidget):
    """Headline + per-part table (min, mean, max, limit, OK/KO). The limit is the
    part's maximum temperature set in the Modèle step; double-clicking a row emits
    ``part_activated(name)``."""

    COLUMNS = ("Pièce", "min", "moy.", "max", "limite", "")
    MAX_COL, LIMIT_COL, STATE_COL = 3, 4, 5
    part_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.headline = QLabel(NO_RESULTS)
        self.headline.setWordWrap(True)
        set_prop(self.headline, role="muted")
        layout.addWidget(self.headline)
        self.table = _table(list(self.COLUMNS))
        self.table.setToolTip("Double-cliquez sur une pièce pour régler sa limite dans "
                              "l’étape Modèle.")
        self.table.cellDoubleClicked.connect(self._double_clicked)
        layout.addWidget(self.table)
        self.rows: list[PartRow] = []
        self.temp_to_display: Callable[[float], float] = lambda c: c

    def clear(self, text: str = NO_RESULTS) -> None:
        self.headline.setText(text)
        self.set_rows([])

    def set_rows(self, rows: list[PartRow]) -> None:
        self.rows = list(rows)
        t = self.table
        t.setRowCount(len(rows))
        conv = self.temp_to_display
        for r, row in enumerate(rows):
            status = {"ok": "✓", "ko": "✗"}.get(row.status, NA)
            values = [row.name, fr(conv(row.t_min)), fr(conv(row.t_mean)), fr(conv(row.t_max)),
                      fr(conv(row.limit), 0) if row.limit is not None else NA, status]
            for col, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                if col:
                    item.setTextAlignment(RIGHT)
                if col == 0:
                    item.setToolTip(f"{row.name} · {fr(row.power_w)} W")
                if col == self.LIMIT_COL:
                    item.setToolTip("Température max admissible, réglée dans l’étape Modèle")
                if col == self.STATE_COL and row.status:
                    item.setForeground(QColor(COLORS["success"] if row.status == "ok"
                                              else COLORS["error_text"]))
                    item.setToolTip("Sous la limite" if row.status == "ok" else
                                    "Au-dessus de la limite")
                if col == self.MAX_COL and row.status == "ko":
                    item.setForeground(QColor(COLORS["error_text"]))
                t.setItem(r, col, item)
        _fit_height(t)

    def _double_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self.rows):
            self.part_activated.emit(self.rows[row].name)


class StaleBanner(QFrame):
    """'Résultats obtenus avec une version précédente du modèle' + what changed,
    Relancer, Voir quand même."""

    def __init__(self, on_rerun, on_dismiss, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("staleBanner")
        self.setStyleSheet(f"#staleBanner {{ background: {COLORS['warning_soft']};"
                           f" border: 1px solid {COLORS['warning']}; border-radius: 10px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        self.label = QLabel("Résultats obtenus avec une version précédente du modèle.")
        self.label.setWordWrap(True)
        self.label.setStyleSheet("font-weight: 600;")
        lay.addWidget(self.label)
        self.changes_label = QLabel("")
        self.changes_label.setWordWrap(True)
        lay.addWidget(self.changes_label)
        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        set_prop(self.details, role="muted")
        self.details.setVisible(False)
        lay.addWidget(self.details)
        self.changes_button = QPushButton("Voir les changements")
        set_prop(self.changes_button, kind="flat")
        self.changes_button.setCheckable(True)
        self.changes_button.toggled.connect(self._toggle_details)
        self.rerun_button = QPushButton("Relancer")
        set_prop(self.rerun_button, kind="primary")
        self.rerun_button.clicked.connect(on_rerun)
        self.dismiss_button = QPushButton("Voir quand même")
        self.dismiss_button.clicked.connect(on_dismiss)
        set_prop(self.dismiss_button, kind="flat")
        lay.addWidget(self.rerun_button)
        lay.addWidget(self.changes_button, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.dismiss_button, 0, Qt.AlignmentFlag.AlignLeft)

    def set_changes(self, changes, rerun_text: str) -> None:
        from .data import changes_line

        line = changes_line(changes)
        self.changes_label.setText(line[:1].upper() + line[1:] + "." if line else "")
        self.changes_label.setVisible(bool(line))
        lines = []
        for c in changes:
            lines.append(f"• {c.text}")
            lines.extend(f"    {d}" for d in c.details[:6])
        self.details.setText("\n".join(lines))
        self.changes_button.setVisible(bool(changes))
        self.rerun_button.setText(rerun_text)

    def _toggle_details(self, on: bool) -> None:
        self.details.setVisible(on)
        self.changes_button.setText("Masquer les changements" if on else "Voir les changements")


class EmptyState(QFrame):
    """Before the first run: what to do, the time it takes, the quick estimate."""

    def __init__(self, on_run, on_volume, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(10)
        title = QLabel("Pas encore de résultats")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        lay.addWidget(title)
        self.text = QLabel("")
        self.text.setWordWrap(True)
        set_prop(self.text, role="muted")
        lay.addWidget(self.text)
        self.run_button = QPushButton("▶  Lancer la simulation")
        set_prop(self.run_button, kind="primary")
        self.run_button.clicked.connect(on_run)
        self.volume_button = QPushButton("Revoir le volume d’air")
        self.volume_button.clicked.connect(on_volume)
        lay.addWidget(self.run_button)
        lay.addWidget(self.volume_button)
        self.hottest = QLabel("")
        self.hottest.setWordWrap(True)
        lay.addWidget(self.hottest)

    def set_info(self, duration: str | None, can_run: bool, hottest: str | None) -> None:
        t = ("Lancez une simulation pour voir les flux d’air, les températures de chaque "
             "pièce et le rapport.")
        if duration:
            t += f" Comptez environ {duration}."
        self.text.setText(t)
        self.run_button.setEnabled(can_run)
        self.hottest.setText(f"<span style='color:{COLORS['warning_soft_text']};"
                             f"font-weight:600'>Estimation rapide</span> · pièce la plus "
                             f"chaude : <b>{hottest}</b>" if hottest else "")
        self.hottest.setVisible(bool(hottest))


class Kpis(QWidget):
    """Key numbers of a run: hottest part, air max, outlet, speed, pressure drop."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        cap = QLabel("Pièce la plus chaude")
        set_prop(cap, role="muted")
        lay.addWidget(cap)
        self.hottest = QLabel(NA)
        self.hottest.setStyleSheet("font-size: 22px; font-weight: 700;")
        lay.addWidget(self.hottest)
        self.vs_estimate = QLabel("")
        self.vs_estimate.setStyleSheet(f"color: {COLORS['warning_text']}; font-size: 12px;")
        lay.addWidget(self.vs_estimate)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(2)
        self.values: dict[str, QLabel] = {}
        for i, key in enumerate(("Air max", "Sortie moyenne", "Vitesse max", "Perte de charge")):
            c = QLabel(key)
            set_prop(c, role="muted")
            v = QLabel(NA)
            v.setStyleSheet("font-size: 15px; font-weight: 600;")
            grid.addWidget(c, (i // 2) * 2, i % 2)
            grid.addWidget(v, (i // 2) * 2 + 1, i % 2)
            self.values[key] = v
        lay.addLayout(grid)
        self.limits = QLabel("")
        self.limits.setWordWrap(True)
        self.limits.setStyleSheet(f"color: {COLORS['error_text']};")
        lay.addWidget(self.limits)

    def set_summary(self, s, fmt_t, estimate_max: float | None, over: list[PartRow]) -> None:
        hot = s.hottest() if hasattr(s, "hottest") else None
        self.hottest.setText(f"{hot.name} · {fmt_t(hot.t_max_c)}" if hot else NA)
        if hot and estimate_max is not None and finite(estimate_max):
            d = hot.t_max_c - estimate_max
            self.vs_estimate.setText(f"{fr(d, 0, sign=True)} °C par rapport à l’estimation rapide")
        else:
            self.vs_estimate.setText("")
        self.vs_estimate.setVisible(bool(self.vs_estimate.text()))
        self.values["Air max"].setText(fmt_t(s.air_t_max_c))
        self.values["Sortie moyenne"].setText(fmt_t(s.outlet_t_mean_c))
        self.values["Vitesse max"].setText(f"{fr(s.air_v_max, 2)} m/s")
        self.values["Perte de charge"].setText(f"{fr(s.pressure_drop_pa, 1)} Pa")
        self.limits.setText("\n".join(
            f"⊗ {r.name} dépasse sa limite ({fr(r.t_max)} / {fr(r.limit, 0)} °C)" for r in over))
        self.limits.setVisible(bool(over))


def energy_line(s) -> str:
    """'15,0 W injectés → air 13,6 W · parois 1,2 W · écart 0,2 W'"""
    b = getattr(s, "energy", None)
    if b is not None:
        power = b.total_power_w
        air = sum(b.air_to_openings_w.values())
        walls = b.heat_out_w - air
        res = b.residual_w
        err = b.error
    else:
        power = getattr(s, "total_power_w", 0.0)
        air = getattr(s, "heat_to_air_w", float("nan"))
        walls = getattr(s, "heat_through_walls_w", 0.0) or 0.0
        res = power - (air if finite(air) else 0.0) - walls
        err = getattr(s, "energy_balance_error", float("nan"))
    if not power:
        return "Bilan d’énergie non disponible."
    text = f"{fr(power)} W injectés → air {fr(air)} W · parois {fr(walls)} W · écart {fr(res)} W"
    if finite(err):
        text += f" ({fr(100 * err, 1, sign=True)} %)"
    return text


class CompareView(QWidget):
    """Two runs side by side: max per part, deltas (B − A), changed parameters."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.header = QLabel("")
        self.header.setWordWrap(True)
        lay.addWidget(self.header)
        self.scale_label = QLabel("")
        set_prop(self.scale_label, role="muted")
        self.scale_label.setWordWrap(True)
        lay.addWidget(self.scale_label)
        self.table = _table(["Max par pièce", "A", "B", "Écart"])
        lay.addWidget(self.table)
        sec = QLabel("PARAMÈTRES MODIFIÉS")
        set_prop(sec, role="section")
        lay.addWidget(sec)
        self.params = _table(["Paramètre", "A", "B"])
        lay.addWidget(self.params)
        self.no_params = QLabel("Aucun : mêmes réglages.")
        set_prop(self.no_params, role="muted")
        lay.addWidget(self.no_params)
        self.diff_button = QPushButton("Afficher la différence")
        self.diff_button.setCheckable(True)
        self.swap_button = QPushButton("Changer de calculs ▾")
        self.exit_button = QPushButton("Quitter la comparaison")
        set_prop(self.exit_button, kind="flat")
        lay.addWidget(self.diff_button)
        lay.addWidget(self.swap_button)
        lay.addWidget(self.exit_button, 0, Qt.AlignmentFlag.AlignLeft)

    def set_comparison(self, title_a: str, title_b: str, rows: list[CompareRow],
                       params: list[tuple[str, str, str]], scale: str) -> None:
        self.header.setText(f"<b>A</b> {title_a}<br><b>B</b> {title_b}")
        self.scale_label.setText(scale)
        t = self.table
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cells = (row.label, *row.cells())
            for c, v in enumerate(cells):
                item = QTableWidgetItem(v)
                if c:
                    item.setTextAlignment(RIGHT)
                if c == 3 and finite(row.delta) and row.unit == "°C" and abs(row.delta) >= 0.05:
                    item.setForeground(QColor(COLORS["success"] if row.delta < 0
                                              else COLORS["error_text"]))
                t.setItem(r, c, item)
        _fit_height(t)
        p = self.params
        p.setRowCount(len(params))
        for r, (k, a, b) in enumerate(params):
            for c, v in enumerate((k, a, b)):
                item = QTableWidgetItem(v)
                if c:
                    item.setTextAlignment(RIGHT)
                p.setItem(r, c, item)
        _fit_height(p)
        p.setVisible(bool(params))
        self.no_params.setVisible(not params)
