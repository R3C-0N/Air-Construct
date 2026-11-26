"""Step 5, Résultats: key numbers, per-part table, 3D fields, report.

Owned by: thread "Résultats" (see docs/ui-architecture.md). Foundation state: the
results table and field choice of the previous window, the quick estimate before
the first run, and the stale-results banner.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...view import FIELDS, result_items
from ..base import StepTab

NO_RESULTS = "Pas encore de résultats. Lancez le calcul dans l’étape Simulation."


def _fmt(v: float) -> str:
    return f"{v:.1f}" if math.isfinite(v) else "n/d"


class ResultsPanel(QWidget):
    """Key numbers of a finished run (or of the quick estimate)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.headline = QLabel(NO_RESULTS)
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Pièce", "Puissance (W)", "T max (°C)",
                                              "T moy. (°C)"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

    def clear(self, text: str = NO_RESULTS) -> None:
        self.headline.setText(text)
        self.table.setRowCount(0)

    def _rows(self, rows: list[list[str]]) -> None:
        self.table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for col, v in enumerate(values):
                item = QTableWidgetItem(v)
                if col:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, col, item)

    def show_estimate(self, case, temps: dict[str, float]) -> None:
        """Instant lumped estimate (no CFD), shown until a run gives real numbers."""
        self.headline.setText(
            "Estimation rapide (sans CFD, approximative et souvent optimiste). "
            "Lancez le calcul pour la simulation."
        )
        power = {c.name: c.power for c in case.components}
        rows = sorted(temps.items(), key=lambda kv: -kv[1] if math.isfinite(kv[1]) else math.inf)
        self._rows([[n, _fmt(power.get(n, 0.0)), _fmt(t), "–"] for n, t in rows])

    def show_summary(self, summary) -> None:
        text = (
            f"Air max {summary.air_t_max_c:.1f} °C · sortie moyenne "
            f"{summary.outlet_t_mean_c:.1f} °C · vitesse max {summary.air_v_max:.2f} m/s"
            f" · perte de charge {summary.pressure_drop_pa:.2f} Pa"
        )
        flow = getattr(summary, "mass_flow_kg_s", float("nan"))
        if math.isfinite(flow):
            text += f" · débit d’air {flow * 1000:.2f} g/s"
        bal = getattr(summary, "energy_balance_error", float("nan"))
        if math.isfinite(bal):
            text += f" · bilan d’énergie {bal * 100:+.0f} %"
        warnings = getattr(summary, "warnings", None) or []
        if warnings:
            text += "\nAvertissements : " + "; ".join(warnings)
        self.headline.setText(text)
        rows = sorted(summary.components,
                      key=lambda c: -c.t_max_c if math.isfinite(c.t_max_c) else math.inf)
        self._rows([[c.name, _fmt(c.power_w), _fmt(c.t_max_c), _fmt(c.t_mean_c)] for c in rows])


class StaleBanner(QFrame):
    """'Results come from a previous version of the model' with Relancer / Voir quand même."""

    def __init__(self, on_rerun, on_dismiss, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("staleBanner")
        self.setStyleSheet("#staleBanner { background: #FEF3C7; border: 1px solid #F59E0B;"
                           " border-radius: 4px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        self.label = QLabel("Résultats obtenus avec une version précédente du modèle.")
        self.label.setWordWrap(True)
        lay.addWidget(self.label, 1)
        self.rerun_button = QPushButton("Relancer")
        self.rerun_button.clicked.connect(on_rerun)
        self.dismiss_button = QPushButton("Voir quand même")
        self.dismiss_button.clicked.connect(on_dismiss)
        lay.addWidget(self.rerun_button)
        lay.addWidget(self.dismiss_button)


class ResultsTab(StepTab):
    key = "results"
    title = "Résultats"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.last_report: Path | None = None
        self.show_geometry = False
        self._stale_dismissed = False

        self.results = ResultsPanel()
        self.banner = StaleBanner(self.rerun, self.dismiss_stale)
        self.banner.setVisible(False)
        self.report_button = QPushButton("Enregistrer le rapport HTML…")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.save_report)

        self._right = QWidget()
        rl = QVBoxLayout(self._right)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.addWidget(QLabel("<b>Synthèse</b>"))
        rl.addWidget(self.banner)
        rl.addWidget(self.results, 1)
        rl.addWidget(self.report_button)

        self.field_combo = QComboBox()
        for key, label in FIELDS.items():
            self.field_combo.addItem(label, key)
        self.field_combo.setEnabled(False)
        self.field_combo.currentIndexChanged.connect(self._field_chosen)
        self.geometry_button = QPushButton("Géométrie")
        self.geometry_button.setCheckable(True)
        self.geometry_button.toggled.connect(self._geometry_toggled)
        self._toolbar = QWidget()
        tl = QHBoxLayout(self._toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("Afficher :"))
        tl.addWidget(self.field_combo)
        tl.addWidget(self.geometry_button)
        tl.addStretch(1)

        ctx.project.results_changed.connect(self._on_results)
        ctx.project.stale_changed.connect(self._on_stale)
        ctx.project.changed.connect(self._refresh_estimate)
        ctx.project.replaced.connect(self._reset_view_state)
        self._on_results()

    # ---- panels -----------------------------------------------------------
    def right_panel(self) -> QWidget:
        return self._right

    def viewport_toolbar(self) -> QWidget:
        return self._toolbar

    def scene(self):
        rec = self.ctx.project.results
        if self.show_geometry or rec is None or not rec.outcome.regions:
            return None
        return result_items(rec.case, rec.outcome.regions, self.field_combo.currentData())

    # ---- state ------------------------------------------------------------
    def _reset_view_state(self) -> None:
        self._stale_dismissed = False
        self.geometry_button.setChecked(False)

    def _on_results(self) -> None:
        rec = self.ctx.project.results
        self._stale_dismissed = False
        if rec is None:
            self.field_combo.setEnabled(False)
            self.report_button.setEnabled(False)
            self._refresh_estimate()
        else:
            out = rec.outcome
            if out.summary is not None:
                self.results.show_summary(out.summary)
            elif out.errors:
                self.results.clear("Pas de résultats : " + out.errors[0])
            else:
                self.results.clear("Pas de résultats.")
            self.field_combo.setEnabled(bool(out.regions))
            self.report_button.setEnabled(out.success)
        self._on_stale(self.ctx.project.stale)
        self.scene_changed.emit()

    def _on_stale(self, stale: bool) -> None:
        if not stale:
            self._stale_dismissed = False
        self.banner.setVisible(stale and not self._stale_dismissed)

    def _refresh_estimate(self) -> None:
        if self.ctx.project.results is not None or self.ctx.runner.running:
            return
        case = self.ctx.project.case
        if case is None:
            self.results.clear()
            return
        try:
            temps = self.ctx.services.quick_temperatures(case)
        except NotImplementedError:
            self.results.clear()
            return
        except Exception as exc:  # noqa: BLE001
            self.results.clear(f"Pas d’estimation rapide : {exc}")
            return
        self.results.show_estimate(case, temps)

    def rerun(self) -> None:
        self.ctx.runner.start()

    def dismiss_stale(self) -> None:
        self._stale_dismissed = True
        self.banner.setVisible(False)

    def _field_chosen(self, *_args) -> None:
        self.scene_changed.emit()

    def _geometry_toggled(self, on: bool) -> None:
        self.show_geometry = on
        self.scene_changed.emit()

    # ---- report -----------------------------------------------------------
    def save_report(self) -> None:
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
