"""Step 4, Simulation: run, stop, live progress and solver log.

Owned by: thread "Simulation" (see docs/ui-architecture.md). Foundation state: the
run controls and log of the previous window, driven by ``ctx.runner``.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from superqt import QCollapsible

from ...alerts import ERROR
from ..base import StepTab


class SimulationTab(StepTab):
    key = "simulation"
    title = "Simulation"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        r = ctx.runner
        self.run_button = QPushButton("Lancer le calcul")
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self.start)
        self.stop_button = QPushButton("Arrêter")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(r.stop)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("Prêt")
        self.residual_label = QLabel("")
        self.blocked_label = QLabel("")
        self.blocked_label.setWordWrap(True)
        self.blocked_label.setStyleSheet("color: #B91C1C;")

        self._left = QWidget()
        lay = QVBoxLayout(self._left)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(QLabel("<b>Simulation</b>"))
        buttons = QHBoxLayout()
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.stop_button)
        lay.addLayout(buttons)
        lay.addWidget(self.progress)
        lay.addWidget(self.residual_label)
        lay.addWidget(self.blocked_label)
        lay.addStretch(1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText("Le journal du solveur s’affiche ici.")
        self.log.setMinimumHeight(300)
        self.log_box = QCollapsible("Journal")
        self.log_box.addWidget(self.log)
        self.log_box.expand(animate=False)
        self._right = QWidget()
        rl = QVBoxLayout(self._right)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.addWidget(self.log_box)
        rl.addStretch(1)

        r.started.connect(self._on_started)
        r.log.connect(self.log.appendPlainText)
        r.progress.connect(self._on_progress)
        r.residual.connect(self._on_residual)
        r.running_changed.connect(lambda _running: self._update_buttons())
        ctx.alerts.changed.connect(self._update_buttons)
        self._update_buttons()

    def left_panel(self) -> QWidget:
        return self._left

    def right_panel(self) -> QWidget:
        return self._right

    def start(self) -> None:
        self.ctx.runner.start()

    def _on_started(self) -> None:
        self.log.clear()
        self.residual_label.setText("")
        self.ctx.goto_tab(self.key)

    def _on_progress(self) -> None:
        r = self.ctx.runner
        self.progress.setRange(0, r.progress_max)
        if r.progress_max > 0:
            self.progress.setValue(min(r.progress_value, r.progress_max))
        self.progress.setFormat(r.progress_text)

    def _on_residual(self, _iteration: int, residuals: dict) -> None:
        if residuals:
            worst = max(residuals, key=lambda k: residuals[k])
            self.residual_label.setText(f"résidu max {worst} : {residuals[worst]:.2e}")

    def _update_buttons(self) -> None:
        running = self.ctx.runner.running
        errors = [a for a in self.ctx.alerts.alerts if a.level == ERROR]
        self.run_button.setEnabled(not running and self.ctx.project.case is not None
                                    and not errors)
        self.stop_button.setEnabled(running)
        self.blocked_label.setText(
            "" if not errors else f"Le cas n’est pas valide : {errors[0].message}"
        )
