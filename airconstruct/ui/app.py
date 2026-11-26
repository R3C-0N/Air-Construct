"""Ultra simple desktop window.

Owned by: workstream "ui". Depends only on case.Case, runner.run_case,
results.summarize/load_regions and estimate.estimate_runtime_s.

Layout: left = form (enclosure size, components table, openings table, precision
slider with estimated time, ambient temperature), centre = 3D view (geometry before
the run, temperature/velocity after) with the results table under it, bottom = log
+ Run/Stop buttons. Runs in a QThread so the window stays responsive.

Start it with ``airconstruct-gui [case.yaml]`` or ``python -m airconstruct.ui``.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError
from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..case import Case
from .form import CaseForm, FormError, format_duration
from .view import FIELDS, SceneView
from .worker import RunOutcome, RunWorker

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "electronics_box.yaml"


def default_case() -> Case:
    """The bundled example when present (source checkout), else a minimal box."""
    if EXAMPLE.exists():
        try:
            return Case.load(EXAMPLE)
        except Exception:  # noqa: BLE001, S110 - fall back to the built-in case
            pass
    return Case.model_validate({
        "name": "new_case",
        "enclosure": {"x": 0.2, "y": 0.12, "z": 0.06},
        "components": [{
            "name": "chip", "material": "silicon", "power": 10.0,
            "box": {"origin": {"x": 0.08, "y": 0.045, "z": 0.0},
                    "size": {"x": 0.03, "y": 0.03, "z": 0.005}},
        }],
        "openings": [
            {"name": "inlet", "kind": "inlet", "face": "xmin", "center": [0.06, 0.03],
             "size": [0.08, 0.04], "flow_rate": 0.0032},
            {"name": "outlet", "kind": "outlet", "face": "xmax", "center": [0.06, 0.03],
             "size": [0.08, 0.04]},
        ],
    })


def validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for e in exc.errors():
            loc = ".".join(str(p) for p in e.get("loc", ()))
            msg = e.get("msg", "").removeprefix("Value error, ")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts)
    return str(exc)


def _default_estimate(case: Case) -> float:
    from ..estimate import estimate_runtime_s

    return estimate_runtime_s(case)


def _default_write_report(case: Case, case_dir: Path, out_dir: Path, summary=None) -> Path:
    from ..results import write_report

    return write_report(case, case_dir, out_dir, summary=summary)


def _fmt(v: float) -> str:
    return f"{v:.1f}" if math.isfinite(v) else "n/a"


class ResultsPanel(QWidget):
    """Key numbers of a finished run."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.headline = QLabel("No results yet. Press Run.")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Component", "Power (W)", "T max (°C)",
                                              "T mean (°C)"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

    def clear(self, text: str = "No results yet. Press Run.") -> None:
        self.headline.setText(text)
        self.table.setRowCount(0)

    def show_summary(self, summary) -> None:
        text = (
            f"Air max {summary.air_t_max_c:.1f} °C · outlet mean {summary.outlet_t_mean_c:.1f} °C"
            f" · max velocity {summary.air_v_max:.2f} m/s"
            f" · pressure drop {summary.pressure_drop_pa:.2f} Pa"
        )
        flow = getattr(summary, "mass_flow_kg_s", float("nan"))
        if math.isfinite(flow):
            text += f" · air flow {flow * 1000:.2f} g/s"
        bal = getattr(summary, "energy_balance_error", float("nan"))
        if math.isfinite(bal):
            text += f" · energy balance {bal * 100:+.0f} %"
        warnings = getattr(summary, "warnings", None) or []
        if warnings:
            text += "\nWarnings: " + "; ".join(warnings)
        self.headline.setText(text)
        rows = sorted(summary.components,
                      key=lambda c: -c.t_max_c if math.isfinite(c.t_max_c) else math.inf)
        self.table.setRowCount(len(rows))
        for r, c in enumerate(rows):
            for col, v in enumerate(
                [c.name, _fmt(c.power_w), _fmt(c.t_max_c), _fmt(c.t_mean_c)]
            ):
                item = QTableWidgetItem(v)
                if col:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, col, item)


class MainWindow(QMainWindow):
    def __init__(
        self,
        case: Case | None = None,
        case_path: Path | None = None,
        work_dir: Path | None = None,
        run_fn: Callable | None = None,
        summarize_fn: Callable | None = None,
        load_regions_fn: Callable | None = None,
        estimate_fn: Callable[[Case], float] | None = None,
        write_report_fn: Callable | None = None,
        open_report: bool = True,
    ) -> None:
        super().__init__()
        self.settings = QSettings("AirConstruct", "AirConstruct")
        self.case_path = case_path
        self.work_dir = Path(
            work_dir or self.settings.value("work_dir", str(Path.home() / "AirConstruct" / "runs"))
        )
        self._run_fn, self._summarize_fn, self._load_fn = run_fn, summarize_fn, load_regions_fn
        self._estimate_fn = estimate_fn or _default_estimate
        self._write_report_fn = write_report_fn or _default_write_report
        self._open_report = open_report
        self.last_report: Path | None = None
        self.thread: QThread | None = None
        self.worker: RunWorker | None = None
        self.outcome: RunOutcome | None = None
        self.current_case: Case | None = None
        self.last_run_case: Case | None = None

        self.resize(1400, 850)
        self._build_ui()
        self._build_menu()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self.refresh)
        self.form.changed.connect(self._refresh_timer.start)

        self.form.set_case(case or default_case())
        self.refresh(reset_camera=True)
        self._update_title()

    # ------------------------------------------------------------------ layout
    def _build_ui(self) -> None:
        self.form = CaseForm()
        scroll = QScrollArea()
        scroll.setWidget(self.form)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(520)

        self.view = SceneView()
        self.field_combo = QComboBox()
        for key, label in FIELDS.items():
            self.field_combo.addItem(label, key)
        self.field_combo.setEnabled(False)
        self.field_combo.currentIndexChanged.connect(self._show_field)
        self.geometry_button = QPushButton("Show geometry")
        self.geometry_button.clicked.connect(lambda: self.refresh(force_geometry=True))
        self.report_button = QPushButton("Save HTML report…")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.save_report)
        view_bar = QHBoxLayout()
        view_bar.addWidget(QLabel("Show:"))
        view_bar.addWidget(self.field_combo)
        view_bar.addWidget(self.geometry_button)
        view_bar.addStretch(1)
        view_bar.addWidget(self.report_button)

        self.results = ResultsPanel()
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addLayout(view_bar)
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.addWidget(self.view)
        vsplit.addWidget(self.results)
        vsplit.setSizes([600, 200])
        cl.addWidget(vsplit)

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(scroll)
        top.addWidget(centre)
        top.setSizes([560, 840])

        # bottom: run controls + log
        self.run_button = QPushButton("Run")
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self.start_run)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_run)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Idle")
        self.residual_label = QLabel("")
        controls = QHBoxLayout()
        controls.addWidget(self.run_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.progress, 1)
        controls.addWidget(self.residual_label)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText("Solver output appears here.")
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addLayout(controls)
        bl.addWidget(self.log)

        main = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(top)
        main.addWidget(bottom)
        main.setSizes([720, 130])
        self.setCentralWidget(main)
        self.status = self.statusBar()

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        for text, key, slot in [
            ("&New", QKeySequence.StandardKey.New, self.new_case),
            ("&Open…", QKeySequence.StandardKey.Open, self.open_case),
            ("&Save", QKeySequence.StandardKey.Save, self.save_case),
            ("Save &as…", QKeySequence.StandardKey.SaveAs, self.save_case_as),
        ]:
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(slot)
            m.addAction(a)
        m.addSeparator()
        a = QAction("Choose run &folder…", self)
        a.triggered.connect(self.choose_work_dir)
        m.addAction(a)
        m.addSeparator()
        a = QAction("&Quit", self)
        a.setShortcut(QKeySequence.StandardKey.Quit)
        a.triggered.connect(self.close)
        m.addAction(a)

    def _update_title(self) -> None:
        name = self.case_path.name if self.case_path else "unsaved case"
        self.setWindowTitle(f"Air-Construct · {name}")

    # ------------------------------------------------------------------ case
    def refresh(self, reset_camera: bool = False, force_geometry: bool = False) -> None:
        """Validate the form, update estimate and 3D preview."""
        try:
            case = self.form.to_case()
        except (FormError, ValidationError, ValueError) as exc:
            self.current_case = None
            self.run_button.setEnabled(False)
            self.status.showMessage(f"Invalid case: {validation_message(exc)}")
            self.form.set_estimate("Estimated time: –")
            return
        self.current_case = case
        self.run_button.setEnabled(self.thread is None)
        self.status.showMessage(f"Case OK. Runs are saved in {self.work_dir}")
        self.form.set_estimate(self.estimate_text(case))
        if force_geometry or self.outcome is None or not self.outcome.regions:
            self.view.show_case(case, reset_camera)

    def estimate_text(self, case: Case) -> str:
        try:
            s = self._estimate_fn(case)
        except NotImplementedError:
            return "Estimated time: not available yet"
        except Exception as exc:  # noqa: BLE001
            return f"Estimated time: unavailable ({exc})"
        return f"Estimated time: about {format_duration(s)}"

    def _ask_discard_results(self) -> None:
        self.outcome = None
        self.results.clear()
        self.field_combo.setEnabled(False)
        self.report_button.setEnabled(False)

    def load_case(self, path: Path) -> None:
        try:
            case = Case.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Open case", f"Could not open {path}:\n{validation_message(exc)}")
            return
        self.case_path = Path(path)
        self._ask_discard_results()
        self.form.set_case(case)
        self.refresh(reset_camera=True)
        self._update_title()

    def new_case(self) -> None:
        self.case_path = None
        self._ask_discard_results()
        self.form.set_case(default_case())
        self.refresh(reset_camera=True)
        self._update_title()

    def open_case(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open case", "", "Case files (*.yaml *.yml)")
        if path:
            self.load_case(Path(path))

    def save_case(self) -> bool:
        if self.case_path is None:
            return self.save_case_as()
        return self.write_case(self.case_path)

    def save_case_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Save case", "", "Case files (*.yaml)")
        if not path:
            return False
        if not path.lower().endswith((".yaml", ".yml")):
            path += ".yaml"
        return self.write_case(Path(path))

    def write_case(self, path: Path) -> bool:
        try:
            case = self.form.to_case()
        except (FormError, ValidationError, ValueError) as exc:
            QMessageBox.warning(self, "Save case", f"The case is not valid:\n{validation_message(exc)}")
            return False
        case.save(path)
        self.case_path = Path(path)
        self._update_title()
        self.status.showMessage(f"Saved {path}")
        return True

    def choose_work_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Run folder", str(self.work_dir))
        if d:
            self.work_dir = Path(d)
            self.settings.setValue("work_dir", d)
            self.refresh()

    # ------------------------------------------------------------------ run
    def start_run(self) -> None:
        if self.thread is not None:
            return
        self.refresh()
        case = self.current_case
        if case is None:
            return
        self._ask_discard_results()
        self.last_run_case = case
        self.log.clear()
        self.residual_label.setText("")
        self.progress.setRange(0, max(1, case.solver.iterations))
        self.progress.setValue(0)
        self.progress.setFormat("Starting…")
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.form.setEnabled(False)

        self.thread = QThread(self)
        self.worker = RunWorker(case, self.work_dir, self._run_fn, self._summarize_fn,
                                self._load_fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.residual.connect(self._on_residual)
        self.worker.stage.connect(self._on_stage)
        self.worker.finished.connect(self._on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_done)
        self.thread.start()

    def stop_run(self) -> None:
        if self.worker is not None:
            self.worker.request_stop()
            self.stop_button.setEnabled(False)
            self.progress.setFormat("Stopping…")

    def _on_stage(self, stage: str) -> None:
        if stage == "Post-processing":
            self.progress.setRange(0, 0)  # busy indicator
        self.progress.setFormat(f"{stage}… %v / %m")
        self.status.showMessage(stage)

    def _on_residual(self, iteration: int, residuals: dict) -> None:
        if self.progress.maximum() > 0:
            self.progress.setValue(min(iteration, self.progress.maximum()))
        if residuals:
            worst = max(residuals, key=lambda k: residuals[k])
            self.residual_label.setText(f"max residual {worst}: {residuals[worst]:.2e}")

    def _on_finished(self, outcome: RunOutcome) -> None:
        self.outcome = outcome
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if outcome.success else 0)
        r = outcome.result
        if outcome.stopped:
            state = "Stopped"
        elif outcome.success:
            state = "Converged" if getattr(r, "converged", False) else "Finished (not converged)"
        else:
            state = "Failed"
        if r is not None:
            state += f" after {getattr(r, 'iterations', 0)} iterations"
            state += f" in {format_duration(getattr(r, 'wall_time_s', 0.0))}"
        self.progress.setFormat(state)
        self.status.showMessage(state + (": " + outcome.errors[0] if outcome.errors else ""))
        if outcome.summary is not None:
            self.results.show_summary(outcome.summary)
        elif outcome.errors:
            self.results.clear("No results: " + outcome.errors[0])
        if outcome.regions and self.last_run_case is not None:
            self.field_combo.setEnabled(True)
            self._show_field()
        self.report_button.setEnabled(outcome.success)

    def _on_thread_done(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None
        self.stop_button.setEnabled(False)
        self.form.setEnabled(True)
        self.run_button.setEnabled(self.current_case is not None)

    def _show_field(self, *_args) -> None:
        if self.outcome and self.outcome.regions and self.last_run_case is not None:
            self.view.show_results(self.last_run_case, self.outcome.regions,
                                   self.field_combo.currentData())

    def save_report(self) -> None:
        if not (self.outcome and self.outcome.success and self.last_run_case):
            return
        case_dir = Path(self.outcome.result.case_dir)
        self.status.showMessage("Writing report…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            path = self._write_report_fn(self.last_run_case, case_dir, case_dir / "report",
                                         summary=self.outcome.summary)
        except NotImplementedError:
            QMessageBox.information(self, "Report", "HTML reports are not implemented yet.")
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Report", f"Could not write the report:\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.last_report = Path(path)
        self.status.showMessage(f"Report written to {path}")
        if self._open_report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def closeEvent(self, event) -> None:
        if self.thread is not None:
            ans = QMessageBox.question(self, "Quit", "A simulation is running. Stop it and quit?")
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.stop_run()
            self.thread.quit()
            self.thread.wait(10000)
        self.view.close_plotter()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("Air-Construct")
    case_path = Path(argv[1]) if len(argv) > 1 else None
    win = MainWindow()
    if case_path is not None:
        win.load_case(case_path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
