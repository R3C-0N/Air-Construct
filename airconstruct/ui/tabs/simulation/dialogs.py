"""The two error dialogs of the Simulation step: "OpenFOAM est introuvable" and "Le calcul
a échoué". Non-blocking (``open()``), so the window and the tests keep running.

Owned by: thread "Simulation".
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...theme import set_prop
from .errors import FailureReport, OpenFoamHelp


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
    """OpenFOAM (or WSL) missing: what to install, with the commands to copy."""

    def __init__(self, help_: OpenFoamHelp, recheck: Callable[[], tuple[bool, str]],
                 set_bashrc: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenFOAM introuvable")
        self.setMinimumWidth(600)
        self._recheck, self._set_bashrc = recheck, set_bashrc
        self.found: bool = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(12)
        self.header = _header(help_.title, help_.subtitle)
        lay.addWidget(self.header)
        self.intro = QLabel(help_.intro)
        self.intro.setWordWrap(True)
        lay.addWidget(self.intro)
        self.command_rows = [CommandRow(c) for c in help_.commands]
        for row in self.command_rows:
            lay.addWidget(row)
        self.after = QLabel(help_.after)
        self.after.setWordWrap(True)
        self.after.setVisible(bool(help_.after))
        set_prop(self.after, role="muted")
        lay.addWidget(self.after)
        self.bashrc_button = QPushButton("Déjà installé ailleurs ? Indiquer le fichier bashrc "
                                         "d’OpenFOAM…")
        set_prop(self.bashrc_button, kind="flat")
        self.bashrc_button.clicked.connect(self._choose_bashrc)
        lay.addWidget(self.bashrc_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        set_prop(self.status, role="muted")
        lay.addWidget(self.status)
        buttons = QHBoxLayout()
        self.copy_all_button = QPushButton("Copier tout le message")
        self.copy_all_button.clicked.connect(lambda: _copy(help_.as_text()))
        self.close_button = QPushButton("Fermer")
        self.close_button.clicked.connect(self.reject)
        self.recheck_button = QPushButton("Vérifier à nouveau")
        set_prop(self.recheck_button, kind="primary")
        self.recheck_button.clicked.connect(self.check_again)
        buttons.addStretch(1)
        for b in (self.copy_all_button, self.close_button, self.recheck_button):
            buttons.addWidget(b)
        lay.addLayout(buttons)

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
            self.recheck_button.setText("Fermer")
            self.recheck_button.clicked.disconnect()
            self.recheck_button.clicked.connect(self.accept)
        return ok


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
