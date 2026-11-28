"""The two error dialogs of the Simulation step: "OpenFOAM est introuvable" (with the
automatic installation of WSL2 and OpenFOAM) and "Le calcul a échoué". Non-blocking
(``open()``), so the window and the tests keep running. Closing the first one while an
installation runs only hides it: the installation goes on (``shutdown()`` stops it).

Owned by: thread "Simulation".
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ....foam_setup import STEP_LABELS, SetupPlan, SetupResult, restart_windows
from ...theme import set_prop
from .errors import FailureReport, OpenFoamHelp
from .setup import SetupFactory, SetupWorker, default_setup, start


def _copy(text: str) -> None:
    cb = QGuiApplication.clipboard()
    if cb is not None:
        cb.setText(text)


def _header(title: str, subtitle: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("dialogTitle")
    set_prop(t, role="title")
    t.setStyleSheet("font-size: 18px;")
    s = QLabel(subtitle)
    s.setWordWrap(True)
    set_prop(s, role="muted")
    lay.addWidget(t)
    lay.addWidget(s)
    return w


class CommandRow(QFrame):
    """One shell command with a "Copier" button."""

    def __init__(self, command: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.command = command
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.text = QPlainTextEdit(command)
        self.text.setReadOnly(True)
        self.text.setFixedHeight(34)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.copy_button = QPushButton("Copier")
        self.copy_button.clicked.connect(lambda: _copy(command))
        lay.addWidget(self.text, 1)
        lay.addWidget(self.copy_button)


class OpenFoamDialog(QDialog):
    """OpenFOAM (or WSL) missing: install it automatically (with consent), or the commands
    to copy and run by hand."""

    def __init__(self, help_: OpenFoamHelp, recheck: Callable[[], tuple[bool, str]],
                 set_bashrc: Callable[[str], None], parent: QWidget | None = None,
                 plan: SetupPlan | None = None, setup_factory: SetupFactory | None = None,
                 restart: Callable[[], None] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenFOAM introuvable")
        self.setMinimumWidth(600)
        self._recheck, self._set_bashrc = recheck, set_bashrc
        self.plan = plan if plan is not None else SetupPlan("manual")
        self._factory = setup_factory or default_setup
        self._restart = restart or restart_windows
        self.found: bool = False
        self.result: SetupResult | None = None
        self.worker: SetupWorker | None = None
        self.thread: QThread | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(12)
        self.header = _header(help_.title, help_.subtitle)
        lay.addWidget(self.header)

        # -- automatic installation: what will happen, then "Installer automatiquement"
        self.auto_box = QFrame()
        self.auto_box.setObjectName("autoInstall")
        self.auto_box.setStyleSheet("#autoInstall { border: 1px solid palette(mid); "
                                    "border-radius: 8px; }")
        al = QVBoxLayout(self.auto_box)
        al.setContentsMargins(16, 12, 16, 12)
        t = QLabel("Air-Construct peut l’installer pour vous")
        set_prop(t, role="title")
        al.addWidget(t)
        self.consent = QLabel("\n".join(f"{i}. {c}" for i, c in
                                        enumerate(self.plan.consent_steps(), 1)))
        self.consent.setWordWrap(True)
        al.addWidget(self.consent)
        self.notes = QLabel("\n".join(self.plan.consent_notes()))
        self.notes.setWordWrap(True)
        self.notes.setVisible(bool(self.plan.consent_notes()))
        set_prop(self.notes, role="muted")
        al.addWidget(self.notes)
        arow = QHBoxLayout()
        self.manual_button = QPushButton("Je préfère l’installer moi-même")
        set_prop(self.manual_button, kind="flat")
        self.manual_button.clicked.connect(lambda: self._show_manual(True))
        self.install_button = QPushButton("Installer automatiquement")
        set_prop(self.install_button, kind="primary")
        self.install_button.clicked.connect(self.start_install)
        arow.addWidget(self.manual_button)
        arow.addStretch(1)
        arow.addWidget(self.install_button)
        al.addLayout(arow)
        lay.addWidget(self.auto_box)
        self.auto_box.setVisible(self.plan.automatic)

        # -- progress of the automatic installation
        self.progress_box = QWidget()
        pl = QVBoxLayout(self.progress_box)
        pl.setContentsMargins(0, 0, 0, 0)
        self.step_labels: dict[str, QLabel] = {}
        for key in self.plan.steps:
            lab = QLabel()
            self.step_labels[key] = lab
            pl.addWidget(lab)
            self._set_step(key, "pending")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        pl.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        self.log.setMaximumBlockCount(2000)
        pl.addWidget(self.log)
        lay.addWidget(self.progress_box)
        self.progress_box.setVisible(False)

        # -- by hand: the commands to copy
        self.manual_box = QWidget()
        ml = QVBoxLayout(self.manual_box)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(12)
        self.intro = QLabel(help_.intro)
        self.intro.setWordWrap(True)
        ml.addWidget(self.intro)
        self.command_rows = [CommandRow(c) for c in help_.commands]
        for row in self.command_rows:
            ml.addWidget(row)
        self.after = QLabel(help_.after)
        self.after.setWordWrap(True)
        self.after.setVisible(bool(help_.after))
        set_prop(self.after, role="muted")
        ml.addWidget(self.after)
        self.bashrc_button = QPushButton("Déjà installé ailleurs ? Indiquer le fichier bashrc "
                                         "d’OpenFOAM…")
        set_prop(self.bashrc_button, kind="flat")
        self.bashrc_button.clicked.connect(self._choose_bashrc)
        ml.addWidget(self.bashrc_button, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.manual_box)
        self.manual_box.setVisible(not self.plan.automatic)
        if not self.plan.automatic and self.plan.reason:
            self.intro.setText(self.plan.reason + "\n" + help_.intro)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        set_prop(self.status, role="muted")
        lay.addWidget(self.status)
        buttons = QHBoxLayout()
        self.copy_all_button = QPushButton("Copier tout le message")
        self.copy_all_button.clicked.connect(lambda: _copy(help_.as_text()))
        self.cancel_button = QPushButton("Annuler l’installation")
        self.cancel_button.clicked.connect(self.cancel_install)
        self.cancel_button.setVisible(False)
        self.restart_button = QPushButton("Redémarrer maintenant")
        set_prop(self.restart_button, kind="primary")
        self.restart_button.clicked.connect(self._restart_now)
        self.restart_button.setVisible(False)
        self.close_button = QPushButton("Fermer")
        self.close_button.clicked.connect(self.reject)
        self.recheck_button = QPushButton("Vérifier à nouveau")
        set_prop(self.recheck_button, kind="primary")
        self.recheck_button.clicked.connect(self.check_again)
        buttons.addWidget(self.copy_all_button)
        buttons.addStretch(1)
        for b in (self.cancel_button, self.close_button, self.recheck_button,
                  self.restart_button):
            buttons.addWidget(b)
        lay.addLayout(buttons)

    # ---- by hand
    def _show_manual(self, on: bool) -> None:
        self.manual_box.setVisible(on)
        self.manual_button.setVisible(not on)
        self.adjustSize()

    def _choose_bashrc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Fichier etc/bashrc d’OpenFOAM")
        if path:
            self._set_bashrc(path)
            self.check_again()

    def check_again(self) -> bool:
        try:
            ok, msg = self._recheck()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)
        self.found = ok
        self.status.setText(f"✓ Trouvé : {msg}. Vous pouvez relancer le calcul." if ok
                            else f"Toujours introuvable : {msg.splitlines()[0] if msg else ''}")
        if ok:
            self._found_buttons()
        return ok

    def _found_buttons(self) -> None:
        self.recheck_button.setText("Fermer")
        self.recheck_button.clicked.disconnect()
        self.recheck_button.clicked.connect(self.accept)
        self.recheck_button.setVisible(True)
        self.close_button.setVisible(False)

    # ---- automatic
    def _set_step(self, key: str, state: str) -> None:
        lab = self.step_labels.get(key)
        if lab is None:
            return
        mark = {"pending": "○", "running": "◐", "done": "✓", "skipped": "–",
                "failed": "✕"}.get(state, "○")
        lab.setText(f"{mark}  {STEP_LABELS.get(key, key)}")
        lab.setProperty("state", state)
        set_prop(lab, role="muted" if state in ("pending", "skipped") else "")

    def running(self) -> bool:
        return self.thread is not None and self.thread.isRunning()

    def start_install(self) -> None:
        """The user agreed: run the plan in the background."""
        if self.running():
            return
        self.result = None
        self.auto_box.setVisible(False)
        self.manual_box.setVisible(False)
        self.progress_box.setVisible(True)
        for key in self.plan.steps:
            self._set_step(key, "pending")
        self.log.clear()
        self.bar.setRange(0, 0)  # busy
        self.status.setText("Installation en cours… Vous pouvez continuer à travailler.")
        self.cancel_button.setVisible(True)
        self.recheck_button.setVisible(False)
        self.close_button.setText("Masquer")
        self.worker = SetupWorker(self.plan, self._recheck, self._factory)
        self.worker.step.connect(self._on_step)
        self.worker.line.connect(self.log.appendPlainText)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.thread = start(self.worker)

    def _on_step(self, key: str, state: str) -> None:
        self._set_step(key, state)
        if state == "running":
            self.bar.setRange(0, 0)

    def _on_progress(self, frac: float) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(int(frac * 100))

    def cancel_install(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
        self.cancel_button.setEnabled(False)
        self.status.setText("Annulation…")

    def _on_finished(self, result: SetupResult) -> None:
        self.result = result
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(True)
        self.close_button.setText("Fermer")
        self.bar.setRange(0, 100)
        if result.status == "done":
            self.bar.setValue(100)
            self.found = True
            self.status.setText("✓ " + result.message)
            self._found_buttons()
        elif result.status == "reboot":
            self.status.setText(result.message)
            self.restart_button.setVisible(True)
            self.close_button.setText("Plus tard")
        else:
            for key, lab in self.step_labels.items():
                if lab.property("state") == "running":
                    self._set_step(key, "failed")
            self.bar.setValue(0)
            self.status.setText(result.message)
            self.install_button.setText("Réessayer")
            self.auto_box.setVisible(True)
            self.consent.setVisible(False)
            self.notes.setVisible(False)
            if result.status == "failed":
                self._show_manual(True)
            self.recheck_button.setVisible(True)

    def _restart_now(self) -> None:
        self._restart()
        self.accept()

    def wait(self, ms: int = 10000) -> bool:
        return self.thread.wait(ms) if self.thread is not None else True

    def shutdown(self) -> None:
        """Stop the installation (the main window is closing)."""
        if self.running():
            self.cancel_install()
            self.wait(15000)


class FailureDialog(QDialog):
    """A run failed: probable cause, the end of the log, and the part to look at."""

    def __init__(self, report: FailureReport, log_tail: list[str],
                 goto_part: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(report.title)
        self.setMinimumWidth(620)
        self.report = report
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(12)
        lay.addWidget(_header(report.title, report.subtitle))
        text = report.cause + (f"\n{report.hint}" if report.hint else "")
        if report.others:
            text += "\n\nAutres constats :\n" + "\n".join(f"• {o}" for o in report.others)
        self.cause = QLabel(text)
        self.cause.setWordWrap(True)
        self.cause.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.cause)
        self.tail = QPlainTextEdit("\n".join(log_tail))
        self.tail.setReadOnly(True)
        self.tail.setMinimumHeight(140)
        self.tail.setVisible(bool(log_tail))
        lay.addWidget(self.tail, 1)
        buttons = QHBoxLayout()
        self.copy_button = QPushButton(f"Copier les {len(log_tail)} dernières lignes")
        self.copy_button.setEnabled(bool(log_tail))
        self.copy_button.clicked.connect(lambda: _copy("\n".join(log_tail)))
        self.close_button = QPushButton("Fermer")
        self.close_button.clicked.connect(self.reject)
        self.goto_button = QPushButton("Aller à la pièce fautive")
        set_prop(self.goto_button, kind="primary")
        self.goto_button.setVisible(bool(report.part))
        if report.part:
            self.goto_button.setToolTip(f"Sélectionner « {report.part} » dans l’étape Modèle")
            self.goto_button.clicked.connect(lambda: (goto_part(report.part), self.accept()))
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.goto_button)
        lay.addLayout(buttons)
