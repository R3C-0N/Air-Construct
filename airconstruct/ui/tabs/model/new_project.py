""""Nouveau projet" dialog: start from a template, open a file or a recent one.

Owned by: thread "Modèle" (see docs/ui-architecture.md). The shell's Fichier >
Nouveau calls :func:`ask_new_project`.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...theme import COLORS, set_prop
from .library import Template

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class TemplateButton(QPushButton):
    def __init__(self, t: Template, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.template = t
        self.setCheckable(True)
        self.setEnabled(t.enabled)
        self.setMinimumSize(190, 86)
        self.setText(f"{t.title}\n{t.subtitle}")
        self.setToolTip(t.subtitle)
        self.setStyleSheet("QPushButton { text-align: left; padding: 10px 12px; "
                           "font-weight: 500; }")


class NewProjectDialog(QDialog):
    """Result: :attr:`data` (the new project, a dict) or :attr:`open_path`."""

    def __init__(self, templates: list[Template], recent: list[str] | None = None,
                 parent: QWidget | None = None, default: str = "board") -> None:
        super().__init__(parent)
        self.setWindowTitle("Nouveau projet")
        self.setModal(True)
        self.data: dict | None = None
        self.open_path: Path | None = None
        self.buttons: dict[str, TemplateButton] = {}

        left = QVBoxLayout()
        left.setSpacing(12)
        title = QLabel("Nouveau projet")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        left.addWidget(title)
        sub = QLabel("Partez d’un modèle, tout reste modifiable ensuite.")
        set_prop(sub, role="muted")
        left.addWidget(sub)
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, t in enumerate(templates):
            b = TemplateButton(t)
            b.clicked.connect(lambda _=False, k=t.key: self.choose(k))
            b.setAutoExclusive(False)
            grid.addWidget(b, i // 3, i % 3)
            self.buttons[t.key] = b
        left.addLayout(grid)
        left.addStretch(1)
        row = QHBoxLayout()
        name_box = QVBoxLayout()
        lab = QLabel("Nom du projet")
        set_prop(lab, role="muted")
        self.name_edit = QLineEdit()
        self.name_edit.setMinimumWidth(220)
        self.name_edit.textChanged.connect(self._check)
        name_box.addWidget(lab)
        name_box.addWidget(self.name_edit)
        row.addLayout(name_box)
        row.addStretch(1)
        self.cancel_button = QPushButton("Annuler")
        self.cancel_button.clicked.connect(self.reject)
        self.create_button = QPushButton("Créer")
        set_prop(self.create_button, kind="primary")
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(self.create)
        row.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.create_button, 0, Qt.AlignmentFlag.AlignBottom)
        left.addLayout(row)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet(f"color: {COLORS['error_text']}; font-size: 12px;")
        left.addWidget(self.error_label)

        side = QFrame()
        side.setStyleSheet(f"QFrame {{ background: {COLORS['field']}; border-radius: 12px; }}")
        side.setFixedWidth(230)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 14)
        sl.setSpacing(4)
        self.open_button = QPushButton("Ouvrir un fichier…")
        self.open_button.clicked.connect(self._browse)
        sl.addWidget(self.open_button)
        sl.addSpacing(10)
        rec = QLabel("RÉCENTS")
        set_prop(rec, role="section")
        sl.addWidget(rec)
        self.recent_buttons: list[QPushButton] = []
        for p in (recent or [])[:6]:
            b = QPushButton(Path(p).name)
            b.setToolTip(p)
            set_prop(b, kind="flat")
            b.setStyleSheet("text-align: left; font-weight: 500;")
            b.clicked.connect(lambda _=False, path=p: self._open(Path(path)))
            sl.addWidget(b)
            self.recent_buttons.append(b)
        if not recent:
            none = QLabel("Aucun fichier récent")
            set_prop(none, role="muted")
            sl.addWidget(none)
        sl.addStretch(1)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(22, 22, 22, 22)
        outer.setSpacing(18)
        outer.addLayout(left, 1)
        outer.addWidget(side)
        self.current: str | None = None
        self.choose(default if default in self.buttons else templates[0].key)

    def choose(self, key: str) -> None:
        t = self.buttons[key].template
        if not t.enabled:
            return
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        if self.current is None or self.name_edit.text() == self._default_name(self.current):
            self.name_edit.setText(self._default_name(key))
        self.current = key
        self._check()

    def _default_name(self, key: str) -> str:
        return str(self.buttons[key].template.data.get("name") or "nouveau_projet")

    def _check(self) -> bool:
        ok = bool(NAME_RE.match(self.name_edit.text()))
        self.error_label.setText("" if ok else
                                 "Nom : lettres, chiffres, _ et -, en commençant par une lettre")
        self.create_button.setEnabled(ok and self.current is not None)
        return ok

    def create(self) -> None:
        if not self._check():
            return
        self.data = self.buttons[self.current].template.build(self.name_edit.text())
        self.accept()

    def _open(self, path: Path) -> None:
        self.open_path = path
        self.accept()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir un cas", "",
                                              "Cas Air-Construct (*.yaml *.yml)")
        if path:
            self._open(Path(path))


def ask_new_project(parent, example: dict, recent: list[str]) -> dict | Path | None:
    """Show the dialog: the new project data, a file to open, or None (cancelled)."""
    from .library import templates

    dlg = NewProjectDialog(templates(example), recent, parent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.open_path if dlg.open_path is not None else dlg.data
