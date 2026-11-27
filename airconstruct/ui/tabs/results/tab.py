"""Step 5, Résultats: key numbers, per-part table against limits, 3D filters (cuts,
isosurface, vectors, streamlines, particles, A→B line), energy balance, comparison
of two runs, exports and report.

Owned by: thread "Résultats" (see docs/ui-architecture.md). The tab is split into:

- ``data.py``: formatting (decimal comma), per-part rows and limits, model changes,
  comparison numbers, CSV (no widgets)
- ``scene.py``: the filter pipeline turned into scene items (pyvista, no widgets)
- ``charts.py``: per-part bars, A→B curve, Sankey of the energy balance (QPainter)
- ``panels.py``: side panel widgets (empty state, stale banner, key numbers, table,
  comparison); ``filters_panel.py``: the left panel
- ``export.py``: PNG, PDF report, VTK

Runs finished in this session are kept (``history``) so two of them can be compared.
The limit of a part is its ``max_temperature``, set in the Modèle step.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...alerts import WARNING, Alert
from ...run import format_duration
from ...selection import component_key
from ...theme import set_prop
from ...widgets.card import Card
from ..base import StepTab
from . import export, history
from .charts import LineChart, PartChart, SankeyWidget, simple_sankey
from .data import (
    PartRow,
    changed_parameters,
    compare_rows,
    describe_changes,
    estimate_rows,
    finite,
    fr,
    over_limit,
    part_rows,
    write_csv,
)
from .filters_panel import FiltersPanel
from .panels import (
    NO_RESULTS,
    CompareView,
    EmptyState,
    Kpis,
    ResultsPanel,
    StaleBanner,
    energy_line,
)
from .scene import (
    FIELD_LABELS,
    PARTICLES,
    FieldData,
    Filter,
    build_scene,
    default_pipeline,
    difference,
    particle_positions,
    sample_line,
    shifted,
)

FIELDS = FIELD_LABELS
HISTORY_SIZE = 8
SETTINGS_FILTERS = "results/filters"


@dataclass
class RunEntry:
    """A finished run kept for comparison."""

    number: int
    record: object  # project.RunRecord
    when: datetime
    data: FieldData | None = None
    extra: dict = field(default_factory=dict)
    name: str | None = None  # title of a saved run ("Calcul du 26 nov. 09:40")

    @property
    def summary(self):
        return self.record.outcome.summary

    @property
    def title(self) -> str:
        t = self.name or f"Calcul {self.number}"
        flows = [o.flow_rate for o in self.record.case.openings
                 if getattr(o, "flow_rate", None)]
        if flows:
            t += f" · {fr(flows[0] * 3600, 1)} m³/h"
        return t

    @property
    def subtitle(self) -> str:
        return f"{export.fr_date(self.when)} · finesse {self.record.case.precision.level}"


class _FitScrollArea(QScrollArea):
    """Vertical scrolling only: the content is kept as wide as the visible area."""

    def resizeEvent(self, event) -> None:
        w = self.widget()
        if w is not None:
            w.setMaximumWidth(self.viewport().width())
        super().resizeEvent(event)


def _section(title: str) -> QLabel:
    label = QLabel(title.upper())
    set_prop(label, role="section")
    return label


class ResultsTab(StepTab):
    key = "results"
    title = "Résultats"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.last_report: Path | None = None
        self.last_export: Path | None = None
        self.show_geometry = False
        self._stale_dismissed = False
        self.history: list[RunEntry] = []
        self._run_count = 0
        self.compare: tuple[int, int] | None = None  # indexes in history (A, B)
        self.show_difference = False
        self.playing = False
        self.phase = 0.0
        self._estimate_max: float | None = None
        self.rows: list[PartRow] = []
        self._store = None

        self._build_right()
        self._build_toolbar()
        self.filters_panel = FiltersPanel(self)
        self.filters_panel.changed.connect(self._filters_changed)
        self.filters_panel.play_toggled.connect(self.set_playing)
        self.filters_panel.set_filters(self._load_filters())
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)

        p = ctx.project
        p.results_changed.connect(self._on_results)
        p.stale_changed.connect(self._on_stale)
        p.changed.connect(self._on_project_changed)
        p.replaced.connect(self._reset_view_state)
        ctx.units.changed.connect(self._on_units)
        ctx.runner.running_changed.connect(lambda _r: self._update_empty())
        self._on_results()

    # ------------------------------------------------------------------ build
    def _build_right(self) -> None:
        self.results = ResultsPanel()
        self.results.part_activated.connect(self.edit_part)
        self.banner = StaleBanner(self.rerun, self.dismiss_stale)
        self.banner.setVisible(False)
        self.empty = EmptyState(self.rerun, lambda: self.ctx.goto_tab("volume"))
        self.kpis = Kpis()
        self.part_chart = PartChart()
        self.csv_button = QPushButton("CSV")
        set_prop(self.csv_button, kind="flat")
        self.csv_button.setToolTip("Exporter le tableau par pièce (CSV)")
        self.csv_button.clicked.connect(lambda: self.export_csv())
        self.energy_label = QLabel("")
        self.energy_label.setWordWrap(True)
        self.sankey = SankeyWidget()
        self.line_chart = LineChart()
        self.compare_view = CompareView()
        self.compare_view.diff_button.toggled.connect(self.set_difference)
        self.compare_view.exit_button.clicked.connect(self.stop_compare)
        self.compare_view.swap_button.clicked.connect(self._swap_menu)
        self.report_button = QPushButton("Rapport HTML…")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.save_report)
        self.pdf_button = QPushButton("Rapport PDF…")
        set_prop(self.pdf_button, kind="primary")
        self.pdf_button.setEnabled(False)
        self.pdf_button.clicked.connect(lambda: self.export_pdf())

        content = QWidget()
        content.setObjectName("transparent")
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(10)
        lay.addWidget(self.banner)
        lay.addWidget(self.empty)
        lay.addWidget(self.compare_view)
        self.kpi_box = QWidget()
        kl = QVBoxLayout(self.kpi_box)
        kl.setContentsMargins(0, 0, 0, 0)
        kl.addWidget(self.kpis)
        lay.addWidget(self.kpi_box)
        head = QHBoxLayout()
        self.parts_title = _section("Par pièce · °C")
        head.addWidget(self.parts_title)
        head.addStretch(1)
        head.addWidget(self.csv_button)
        self.parts_box = QWidget()
        pl = QVBoxLayout(self.parts_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        pl.addLayout(head)
        pl.addWidget(self.results)
        pl.addWidget(self.part_chart)
        lay.addWidget(self.parts_box)
        self.energy_box = QWidget()
        el = QVBoxLayout(self.energy_box)
        el.setContentsMargins(0, 0, 0, 0)
        el.addWidget(_section("Bilan d’énergie"))
        el.addWidget(self.energy_label)
        el.addWidget(self.sankey)
        lay.addWidget(self.energy_box)
        self.line_box = QWidget()
        ll = QVBoxLayout(self.line_box)
        ll.setContentsMargins(0, 0, 0, 0)
        self.line_title = _section("Le long de A → B")
        ll.addWidget(self.line_title)
        ll.addWidget(self.line_chart)
        lay.addWidget(self.line_box)
        lay.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.pdf_button)
        buttons.addWidget(self.report_button)
        self.report_box = QWidget()
        self.report_box.setLayout(buttons)
        buttons.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.report_box)

        scroll = _FitScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self._right = Card("Synthèse")
        self._right.body.addWidget(scroll, 1)

    def _build_toolbar(self) -> None:
        self.field_combo = QComboBox()
        for key, label in FIELDS.items():
            self.field_combo.addItem(label, key)
        self.field_combo.setEnabled(False)
        self.field_combo.currentIndexChanged.connect(self._field_chosen)
        self.auto_scale = QCheckBox("auto")
        self.auto_scale.setChecked(True)
        self.auto_scale.setToolTip("Échelle des couleurs du minimum au maximum du champ")
        self.scale_min, self.scale_max = QDoubleSpinBox(), QDoubleSpinBox()
        for sb in (self.scale_min, self.scale_max):
            sb.setRange(-1e6, 1e6)
            sb.setDecimals(1)
            sb.setVisible(False)
            sb.setFixedWidth(84)
            sb.valueChanged.connect(lambda _v: self.scene_changed.emit())
        self.auto_scale.toggled.connect(self._auto_scale_toggled)
        self.scale_label = QLabel("")
        set_prop(self.scale_label, role="muted")
        self.geometry_button = QPushButton("Géométrie")
        self.geometry_button.setCheckable(True)
        self.geometry_button.toggled.connect(self._geometry_toggled)

        self.compare_button = QToolButton()
        self.compare_button.setText("Comparer ▾")
        self.compare_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.compare_menu = QMenu(self.compare_button)
        self.compare_menu.aboutToShow.connect(lambda: self._fill_compare_menu(self.compare_menu))
        self.compare_button.setMenu(self.compare_menu)

        self.export_button = QToolButton()
        self.export_button.setText("Exporter ▾")
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        m = QMenu(self.export_button)
        self.export_actions = {
            "png": m.addAction("Image de la vue (PNG)", lambda: self.export_png()),
            "pdf": m.addAction("Rapport complet (PDF)", lambda: self.export_pdf()),
            "html": m.addAction("Rapport complet (HTML)", self.save_report),
            "csv": m.addAction("Tableau par pièce (CSV)", lambda: self.export_csv()),
            "vtk": m.addAction("Champs 3D (VTK)", lambda: self.export_vtk_fields()),
            "vtp": m.addAction("Lignes de courant (VTK)", lambda: self.export_vtk_streamlines()),
        }
        m.addSeparator()
        note = m.addAction("Le rapport contient : résumé, tableau par pièce, coupes T et U, "
                           "lignes de courant, bilan d’énergie.")
        note.setEnabled(False)
        self.export_button.setMenu(m)

        self._toolbar = QWidget()
        tl = QHBoxLayout(self._toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("Afficher :"))
        tl.addWidget(self.field_combo)
        tl.addWidget(self.scale_label)
        tl.addWidget(self.scale_min)
        tl.addWidget(self.scale_max)
        tl.addWidget(self.auto_scale)
        tl.addWidget(self.geometry_button)
        tl.addStretch(1)
        tl.addWidget(self.compare_button)
        tl.addWidget(self.export_button)

    # ------------------------------------------------------------------ panels
    def left_panel(self) -> QWidget:
        return self.filters_panel

    def right_panel(self) -> QWidget:
        return self._right

    def viewport_toolbar(self) -> QWidget:
        return self._toolbar

    def actions(self) -> list[QAction]:
        if not hasattr(self, "_actions"):
            png = QAction("Exporter l’image de la vue", self)
            png.setShortcut(QKeySequence("Ctrl+Shift+E"))
            png.triggered.connect(lambda: self.export_png())
            play = QAction("Animer les particules", self)
            play.setShortcut(QKeySequence("P"))
            play.triggered.connect(lambda: self.set_playing(not self.playing))
            self._actions = [png, play]
        return self._actions

    # ------------------------------------------------------------------ state
    @property
    def field(self) -> str:
        return self.field_combo.currentData() or "T"

    @property
    def case(self):
        rec = self.ctx.project.results
        return rec.case if rec is not None else self.ctx.project.last_valid_case

    @property
    def current(self) -> RunEntry | None:
        rec = self.ctx.project.results
        return next((e for e in reversed(self.history) if e.record is rec), None)

    @property
    def data(self) -> FieldData | None:
        e = self.current
        return e.data if e is not None else None

    @property
    def filters(self) -> list[Filter]:
        return self.filters_panel.filters

    def field_range(self, air_only: bool = False) -> tuple[float, float]:
        d = self.data
        if d is None:
            return (0.0, 1.0)
        return d.range(self.field, d.fluid if air_only else None)

    def clim(self, data: FieldData | None = None) -> tuple[float, float]:
        if not self.auto_scale.isChecked():
            lo, hi = self.scale_min.value(), self.scale_max.value()
            return (lo, hi) if hi > lo else (lo, lo + 1e-6)
        d = data or self.data
        return d.range(self.field) if d is not None else (0.0, 1.0)

    def _reset_view_state(self) -> None:
        self._stale_dismissed = False
        self.geometry_button.setChecked(False)
        self.stop_compare()

    def _on_results(self) -> None:
        rec = self.ctx.project.results
        self._stale_dismissed = False
        if rec is not None and rec.outcome.summary is not None and self.current is None:
            self._run_count += 1
            data = FieldData(rec.case, rec.outcome.regions) if rec.outcome.regions else None
            entry = RunEntry(self._run_count, rec, datetime.now(), data)
            entry.extra["store_id"] = history.record_run(self.store(), rec)
            self.history.append(entry)
            del self.history[:-HISTORY_SIZE]
            self.compare = None
            self._estimate_max = self._estimate_hottest(rec.case)
        if rec is None:
            self.compare = None
        self._update_scale_label()
        self._refresh()
        self._on_stale(self.ctx.project.stale)
        self.scene_changed.emit()

    def _estimate_hottest(self, case) -> float | None:
        try:
            temps = self.ctx.services.quick_temperatures(case)
        except Exception:  # noqa: BLE001 - optional comparison
            return None
        vals = [t for t in temps.values() if finite(t)]
        return max(vals) if vals else None

    def _has_results(self) -> bool:
        rec = self.ctx.project.results
        return rec is not None and rec.outcome.summary is not None

    def _refresh(self) -> None:
        """Fill the side panel for the current mode (empty, results, comparison)."""
        rec = self.ctx.project.results
        has = self._has_results()
        comparing = self.compare is not None and has
        self.empty.setVisible(not has)
        self.compare_view.setVisible(comparing)
        self.kpi_box.setVisible(has and not comparing)
        self.energy_box.setVisible(has and not comparing)
        self.parts_box.setVisible(not comparing)
        self.report_box.setVisible(has)
        self.field_combo.setEnabled(bool(has and rec.outcome.regions))
        self.report_button.setEnabled(bool(rec is not None and rec.outcome.success))
        self.pdf_button.setEnabled(has)
        for k, a in self.export_actions.items():
            a.setEnabled(has and (k not in ("vtk", "vtp") or self.data is not None))
        self._right.set_title("Comparer deux calculs" if comparing else "Synthèse")
        if not has:
            if rec is not None and rec.outcome.errors:
                self.results.clear("Pas de résultats : " + rec.outcome.errors[0])
            elif rec is not None:
                self.results.clear("Pas de résultats.")
            self._refresh_estimate()
            self._update_empty()
            self.line_box.setVisible(False)
            return
        s = rec.outcome.summary
        self.rows = part_rows(s, self.ctx.project.data)
        self.results.headline.setText(self._headline(s))
        self._set_rows(self.rows)
        u = self.ctx.units
        self.kpis.set_summary(s, u.fmt_temperature, self._estimate_max, over_limit(self.rows))
        self.energy_label.setText(energy_line(s))
        b = getattr(s, "energy", None)
        self.sankey.set_sankey(b.to_sankey() if b is not None else simple_sankey(s))
        if comparing:
            self._fill_compare()
        self._update_line_chart()

    def _headline(self, s) -> str:
        text = (f"Air max {fr(s.air_t_max_c)} °C · sortie moyenne {fr(s.outlet_t_mean_c)} °C"
                f" · vitesse max {fr(s.air_v_max, 2)} m/s · perte de charge "
                f"{fr(s.pressure_drop_pa, 2)} Pa")
        flow = getattr(s, "mass_flow_kg_s", math.nan)
        if finite(flow):
            text += f" · débit d’air {fr(flow * 1000, 2)} g/s"
        warnings = getattr(s, "warnings", None) or []
        if warnings:
            text += "\nAvertissements : " + "; ".join(warnings)
        return text

    def _set_rows(self, rows: list[PartRow]) -> None:
        u = self.ctx.units
        self.results.temp_to_display = u.temperature_to_display
        self.parts_title.setText(f"PAR PIÈCE · {u.temperature_symbol.upper()}")
        self.results.set_rows(rows)
        self.part_chart.set_rows(rows)
        self.part_chart.setVisible(any(finite(r.t_max) for r in rows))

    def _update_empty(self) -> None:
        if self._has_results():
            return
        p = self.ctx.project
        duration = None
        if p.case is not None:
            try:
                duration = format_duration(self.ctx.services.estimate_runtime(p.case))
            except Exception:  # noqa: BLE001
                duration = None
        hot = max(self.rows, key=lambda r: r.t_max if finite(r.t_max) else -math.inf,
                  default=None)
        hottest = f"{hot.name} · {self.ctx.units.fmt_temperature(hot.t_max, 0)}" if (
            hot and finite(hot.t_max)) else None
        self.empty.set_info(duration, p.case is not None and not self.ctx.runner.running,
                            hottest)

    def _refresh_estimate(self) -> None:
        if self._has_results() or self.ctx.runner.running:
            return
        if self.ctx.project.results is not None:  # failed run: keep its message
            self._set_rows([])
            return
        case = self.ctx.project.case
        self.rows = []
        if case is None:
            self.results.clear()
            self._set_rows([])
            return
        try:
            temps = self.ctx.services.quick_temperatures(case)
        except NotImplementedError:
            self.results.clear()
            self._set_rows([])
            return
        except Exception as exc:  # noqa: BLE001
            self.results.clear(f"Pas d’estimation rapide : {exc}")
            self._set_rows([])
            return
        self.results.headline.setText(
            "Estimation rapide (sans CFD, approximative et souvent optimiste). "
            "Lancez le calcul pour la simulation.")
        self.rows = estimate_rows(case, temps, self.ctx.project.data)
        self._set_rows(self.rows)

    def _on_project_changed(self) -> None:
        if self._has_results():
            rows = part_rows(self.ctx.project.results.outcome.summary, self.ctx.project.data)
            if [r.limit for r in rows] != [r.limit for r in self.rows]:
                self._refresh()  # a limit was edited
        else:
            self._refresh_estimate()
            self._update_empty()
        if self.ctx.project.stale:
            self._update_banner()

    def _on_stale(self, stale: bool) -> None:
        if not stale:
            self._stale_dismissed = False
        if stale:
            self._update_banner()
        self.banner.setVisible(stale and not self._stale_dismissed)

    def _update_banner(self) -> None:
        rec = self.ctx.project.results
        if rec is None:
            return
        changes = describe_changes(rec.data, self.ctx.project.data)
        text = "Relancer"
        case = self.ctx.project.case
        if case is not None:
            try:
                text += f" · ≈ {format_duration(self.ctx.services.estimate_runtime(case))}"
            except Exception:  # noqa: BLE001, S110 - the estimate is optional
                pass
        self.banner.set_changes(changes, text)

    def _on_units(self) -> None:
        self._refresh()
        self.filters_panel.refresh_texts()
        self.filters_panel._show_editor()

    def rerun(self) -> None:
        self.ctx.runner.start()

    def dismiss_stale(self) -> None:
        self._stale_dismissed = True
        self.banner.setVisible(False)

    # ------------------------------------------------------------------ limits
    def edit_part(self, name: str) -> None:
        """Open the part in the Modèle step (where its limit is set)."""
        self.ctx.selection.set([component_key(name)])
        self.ctx.goto_tab("model")

    def alerts(self, project):
        rec = project.results
        if rec is None or rec.outcome.summary is None:
            return []
        rows = part_rows(rec.outcome.summary, project.data)
        return [Alert(WARNING, f"{r.name} dépasse sa limite ({fr(r.t_max)} / {fr(r.limit, 0)} °C)",
                      self.key, component_key(r.name)) for r in over_limit(rows)]

    # ------------------------------------------------------------------ scene
    def scene(self):
        rec = self.ctx.project.results
        if self.show_geometry or rec is None or not rec.outcome.regions:
            return None
        if self.compare is not None:
            items = self._compare_scene()
            if items is not None:
                return items
        d = self.data
        if d is None:
            return None
        return build_scene(d, self.filters, self.field, self.clim(d), self.phase)

    def _compare_scene(self):
        a, b = (self.history[i] for i in self.compare)
        if a.data is None or b.data is None:
            return None
        fld = self.field
        if self.show_difference:
            diff = difference(a.data, b.data, fld)
            if diff is not None:
                lo, hi = diff.range(fld)
                m = max(abs(lo), abs(hi), 1e-6)
                keep = [f for f in self.filters if f.kind in ("parts", "slice", "iso")]
                return build_scene(diff, keep, fld, (-m, m))
        ra, rb = a.data.range(fld), b.data.range(fld)
        clim = self.clim(a.data) if not self.auto_scale.isChecked() else (
            min(ra[0], rb[0]), max(ra[1], rb[1]))
        keep = [f for f in self.filters if f.kind != "particles"]
        items_a = build_scene(a.data, keep, fld, clim)
        dx = 1.25 * max(a.record.case.enclosure.x, b.record.case.enclosure.x)
        return items_a + shifted(build_scene(b.data, keep, fld, clim), dx, "#B")

    def _filters_changed(self) -> None:
        self._save_filters()
        if not any(f.kind == "particles" and f.visible for f in self.filters):
            self.set_playing(False)
        self._update_line_chart()
        self.scene_changed.emit()

    def _field_chosen(self, *_args) -> None:
        self._update_scale_label()
        self.filters_panel.refresh_texts()
        self.filters_panel._show_editor()
        self._update_line_chart()
        self.scene_changed.emit()

    def _geometry_toggled(self, on: bool) -> None:
        self.show_geometry = on
        self.scene_changed.emit()

    def _auto_scale_toggled(self, on: bool) -> None:
        lo, hi = self.field_range()
        for sb, v in ((self.scale_min, lo), (self.scale_max, hi)):
            sb.blockSignals(True)
            sb.setValue(round(v, 1))
            sb.blockSignals(False)
            sb.setVisible(not on)
        self._update_scale_label()
        self.scene_changed.emit()

    def _update_scale_label(self) -> None:
        if self.data is None or not self.auto_scale.isChecked():
            self.scale_label.setText("")
            return
        lo, hi = self.clim()
        unit = "m/s" if self.field == "U" else "°C"
        d = 2 if self.field == "U" else 0
        self.scale_label.setText(f"{fr(lo, d)} → {fr(hi, d)} {unit}")

    def _update_line_chart(self) -> None:
        line = next((f for f in self.filters if f.kind == "line" and f.visible), None)
        d = self.data
        show = line is not None and d is not None and self.compare is None
        self.line_box.setVisible(show)
        if not show:
            return
        a, b = line.params.get("a"), line.params.get("b")
        if a is None or b is None:
            from .scene import default_line

            a, b = default_line(d.case)
        try:
            dist, vals = sample_line(d, a, b, self.field)
        except Exception:  # noqa: BLE001
            self.line_chart.clear()
            return
        u = self.ctx.units
        name = "température" if self.field == "T" else "vitesse"
        self.line_title.setText(f"LE LONG DE A → B · {name.upper()}")
        self.line_chart.set_data(u.length_to_display(dist), vals, u.length_symbol,
                                 FIELDS[self.field])

    # ---- filters persistence ------------------------------------------------
    def _load_filters(self) -> list[Filter]:
        raw = self.ctx.settings.value(SETTINGS_FILTERS, "")
        try:
            items = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            items = None
        filters = [f for f in (Filter.from_dict(d) for d in items or []) if f is not None]
        return filters or default_pipeline()

    def _save_filters(self) -> None:
        self.ctx.settings.setValue(SETTINGS_FILTERS,
                                   json.dumps([f.to_dict() for f in self.filters]))

    # ---- particles -----------------------------------------------------------
    def set_playing(self, on: bool) -> None:
        on = bool(on) and any(f.kind == "particles" and f.visible for f in self.filters)
        self.playing = on
        if self.filters_panel.play_button is not None:
            b = self.filters_panel.play_button
            b.blockSignals(True)
            b.setChecked(on)
            b.setText("❚❚  Pause" if on else "▶  Animer")
            b.blockSignals(False)
        if on and self.active:
            self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        self.phase = (self.phase + 0.006) % 1.0
        f = next((f for f in self.filters if f.kind == "particles" and f.visible), None)
        d = self.data
        if f is None or d is None:
            return
        item = next((i for i in self.ctx.viewport.items if i.name == PARTICLES), None)
        pts = particle_positions(d.streamlines(36), int(f.params["count"]), self.phase)
        if item is None or item.mesh.n_points != len(pts):
            return
        item.mesh.points = pts
        plotter = self.ctx.viewport.scene.plotter
        if plotter is not None:
            plotter.render()

    def activated(self) -> None:
        if self.playing:
            self._timer.start()

    def deactivated(self) -> None:
        self._timer.stop()

    # ------------------------------------------------------------------ compare
    def store(self):
        """The run history (``history.sqlite`` in the runs folder), opened on first use."""
        root = Path(self.ctx.work_dir())
        if self._store is None or Path(self._store.root) != root:
            self._store = history.open_store(root)
        return self._store

    def _fill_compare_menu(self, menu: QMenu) -> None:
        menu.clear()
        cur = self.current
        others = [e for e in self.history if e is not cur]
        saved = []
        if cur is not None:
            skip = {e.extra.get("store_id") for e in self.history} - {None}
            saved = history.saved_runs(self.store(), cur.record.case.name, skip)
        if cur is None or not (others or saved):
            a = menu.addAction("Lancez au moins deux calculs pour les comparer")
            a.setEnabled(False)
            return
        if others:
            menu.addSection("Calculs de la session")
        for e in reversed(others):
            menu.addAction(f"{e.title} ({e.subtitle})  ↔  {cur.title}",
                           lambda e=e: self.start_compare(e, cur))
        if saved:
            menu.addSection("Calculs enregistrés")
        for r in saved:
            when = export.fr_date(history.run_date(r))
            label = f"Calcul du {when}" + (f" · {r.label}" if r.label and
                                           r.label != history.STORE_LABEL else "")
            menu.addAction(f"{label}  ↔  {cur.title}",
                           lambda r=r: self.compare_saved(r))
        if self.compare is not None:
            menu.addSeparator()
            menu.addAction("Quitter la comparaison", self.stop_compare)

    def _swap_menu(self) -> None:
        m = QMenu(self.compare_view.swap_button)
        self._fill_compare_menu(m)
        if self.compare is not None:
            m.addAction("Inverser A et B", self.swap_compare)
        m.exec(self.compare_view.swap_button.mapToGlobal(
            self.compare_view.swap_button.rect().bottomLeft()))

    def compare_saved(self, rec) -> None:
        """Compare the current run with a run of the history (runs.RunRecord)."""
        cur = self.current
        if cur is None:
            return
        try:
            from ....results import load_regions
        except ImportError:
            load_regions = None
        record = history.as_record(rec, load_regions)
        regions = record.outcome.regions
        when = history.run_date(rec)
        entry = RunEntry(0, record, when, FieldData(record.case, regions) if regions else None,
                         {"store_id": rec.id}, name=f"Calcul du {export.fr_date(when)}")
        self.history.insert(0, entry)
        self.start_compare(entry, cur)

    def start_compare(self, a: RunEntry, b: RunEntry) -> None:
        self.compare = (self.history.index(a), self.history.index(b))
        self.set_playing(False)
        self._refresh()
        self.scene_changed.emit()

    def swap_compare(self) -> None:
        if self.compare is not None:
            self.compare = (self.compare[1], self.compare[0])
            self._refresh()
            self.scene_changed.emit()

    def stop_compare(self) -> None:
        if self.compare is None:
            return
        self.compare = None
        self.show_difference = False
        self.compare_view.diff_button.setChecked(False)
        self._refresh()
        self.scene_changed.emit()

    def set_difference(self, on: bool) -> None:
        self.show_difference = bool(on)
        self.scene_changed.emit()

    def _fill_compare(self) -> None:
        a, b = (self.history[i] for i in self.compare)
        rows = compare_rows(a.summary, b.summary)
        params = changed_parameters(a.record.data, b.record.data)
        fld = self.field
        if a.data is not None and b.data is not None:
            ra, rb = a.data.range(fld), b.data.range(fld)
            unit = "m/s" if fld == "U" else "°C"
            scale = (f"Même échelle {fr(min(ra[0], rb[0]), 0)} → {fr(max(ra[1], rb[1]), 0)} "
                     f"{unit} · B est dessiné à droite de A, mêmes vues.")
            same = difference(a.data, b.data, fld) is not None
            self.compare_view.diff_button.setEnabled(same)
            self.compare_view.diff_button.setToolTip(
                "" if same else "Les deux calculs n’ont pas le même maillage.")
        else:
            scale = "Champs 3D non disponibles pour l’un des calculs."
            self.compare_view.diff_button.setEnabled(False)
        self.compare_view.set_comparison(f"{a.title} · {a.subtitle}",
                                         f"{b.title} · {b.subtitle}", rows, params, scale)

    # ------------------------------------------------------------------ exports
    def _ask_path(self, title: str, name: str, pattern: str) -> Path | None:
        rec = self.ctx.project.results
        base = Path(rec.outcome.result.case_dir) if (rec and rec.outcome.result) else \
            self.ctx.work_dir()
        path, _ = QFileDialog.getSaveFileName(self._right, title, str(base / name), pattern)
        return Path(path) if path else None

    def _done(self, path: Path | None, what: str) -> Path | None:
        if path is not None:
            self.last_export = Path(path)
            self.ctx.show_message(f"{what} enregistré : {path}")
        return path

    def _fail(self, what: str, exc: Exception) -> None:
        QMessageBox.warning(self._right, what, f"Impossible d’exporter :\n{exc}")

    def _name(self, suffix: str) -> str:
        case = self.case
        return f"{case.name if case else 'resultats'}{suffix}"

    def export_csv(self, path: Path | None = None) -> Path | None:
        if not self.rows:
            return None
        path = path or self._ask_path("Tableau par pièce", self._name("_pieces.csv"),
                                      "CSV (*.csv)")
        if path is None:
            return None
        try:
            write_csv(self.rows, path)
        except OSError as exc:
            self._fail("Tableau par pièce", exc)
            return None
        return self._done(path, "Tableau par pièce")

    def export_png(self, path: Path | None = None) -> Path | None:
        path = path or self._ask_path("Image de la vue", self._name("_vue.png"), "PNG (*.png)")
        if path is None:
            return None
        try:
            export.save_view_png(self.ctx.viewport.scene.plotter, self.ctx.viewport.items, path)
        except Exception as exc:  # noqa: BLE001
            self._fail("Image de la vue", exc)
            return None
        return self._done(path, "Image")

    def export_vtk_fields(self, path: Path | None = None) -> Path | None:
        d = self.data
        if d is None:
            return None
        path = path or self._ask_path("Champs 3D", self._name("_champs.vtm"),
                                      "VTK multibloc (*.vtm)")
        if path is None:
            return None
        try:
            path = export.write_fields_vtk(d.regions, path)
        except Exception as exc:  # noqa: BLE001
            self._fail("Champs 3D", exc)
            return None
        return self._done(path, "Champs 3D")

    def export_vtk_streamlines(self, path: Path | None = None) -> Path | None:
        d = self.data
        if d is None:
            return None
        f = next((f for f in self.filters if f.kind == "streamlines"), Filter("streamlines"))
        path = path or self._ask_path("Lignes de courant", self._name("_lignes.vtp"),
                                      "VTK (*.vtp)")
        if path is None:
            return None
        try:
            path = export.write_streamlines_vtk(d.streamlines(int(f.params["seeds"])), path)
        except Exception as exc:  # noqa: BLE001
            self._fail("Lignes de courant", exc)
            return None
        return self._done(path, "Lignes de courant")

    def export_pdf(self, path: Path | None = None) -> Path | None:
        rec = self.ctx.project.results
        if not self._has_results():
            return None
        path = path or self._ask_path("Rapport PDF", self._name("_rapport.pdf"), "PDF (*.pdf)")
        if path is None:
            return None
        self.ctx.show_message("Écriture du rapport PDF…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        view = None
        try:
            plotter = self.ctx.viewport.scene.plotter
            if plotter is not None:
                view = Path(path).with_suffix(".vue.png")
                try:
                    export.save_view_png(plotter, self.ctx.viewport.items, view)
                except Exception:  # noqa: BLE001
                    view = None
            case_dir = None
            if rec.outcome.result is not None and rec.outcome.regions:
                case_dir = Path(rec.outcome.result.case_dir)
            path = export.write_pdf(path, rec.case, rec.outcome.summary, self.rows,
                                    case_dir=case_dir, view_png=view)
        except Exception as exc:  # noqa: BLE001
            self._fail("Rapport PDF", exc)
            return None
        finally:
            QApplication.restoreOverrideCursor()
            if view is not None:
                view.unlink(missing_ok=True)
        self.last_report = Path(path)
        self._done(path, "Rapport PDF")
        if self.ctx.services.open_report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        return path

    def save_report(self) -> None:
        """HTML report of the core (results.write_report), next to the run."""
        rec = self.ctx.project.results
        if not (rec and rec.outcome.success):
            return
        case_dir = Path(rec.outcome.result.case_dir)
        self.ctx.show_message("Écriture du rapport…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            path = self.ctx.services.write_report(rec.case, case_dir, case_dir / "report",
                                                  summary=rec.outcome.summary)
        except NotImplementedError:
            QMessageBox.information(self._right, "Rapport",
                                    "Les rapports HTML ne sont pas encore disponibles.")
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self._right, "Rapport", f"Impossible d’écrire le rapport :\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.last_report = Path(path)
        self.ctx.show_message(f"Rapport écrit dans {path}")
        if self.ctx.services.open_report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


__all__ = ["FIELDS", "NO_RESULTS", "ResultsTab"]
