""""Importer un fichier CAO" dialog: units, bodies found, material, then import.

Owned by: thread "Modèle" (see docs/ui-architecture.md). Reading is
:func:`airconstruct.cad.read`; what the import does to the project is
:mod:`.cadparts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .... import cad
from ....materials import SOLIDS
from ...theme import COLORS, set_prop
from .library import material_label

UNIT_LABELS = {"mm": "millimètres", "cm": "centimètres", "m": "mètres", "in": "pouces"}


@dataclass
class CadImport:
    """What the user chose: the bodies (metres) and the material of the parts."""

    path: Path
    bodies: list[cad.Body]
    material: str

    @property
    def source(self) -> str:
        return self.path.name


def _mm(v: float) -> str:
    return f"{v * 1000:.4g}".replace(".", ",")


class CadImportDialog(QDialog):
    """Reads ``path`` at once; :attr:`error` is set (and Importer disabled) when the
    file cannot be read. Result: :attr:`result_import`."""

    def __init__(self, path: str | Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle("Importer un fichier CAO")
        self.setMinimumWidth(520)
        self.result_import: CadImport | None = None
        self.error: str | None = None
        self._raw: list[cad.Body] = []
        self.bodies: list[cad.Body] = []
        is_stl = self.path.suffix.lower() in cad.STL_SUFFIXES

        lay = QVBoxLayout(self)
        title = QLabel(self.path.name)
        set_prop(title, role="title")
        lay.addWidget(title)
        intro = QLabel("Chaque corps du fichier devient une pièce solide, à sa place. "
                       "La simulation suit sa forme réelle, en escalier sur le maillage "
                       "(plus fin avec la précision).")
        intro.setWordWrap(True)
        set_prop(intro, role="muted")
        lay.addWidget(intro)

        form = QFormLayout()
        self.unit = QComboBox()
        for k, label in UNIT_LABELS.items():
            self.unit.addItem(label, k)
        self.unit.setEnabled(is_stl)
        self.unit.setToolTip("Un fichier STL ne dit pas son unité." if is_stl else
                             "Un fichier STEP donne son unité : elle est utilisée.")
        if not is_stl:
            self.unit.clear()
            self.unit.addItem("celle du fichier", None)
        self.unit.currentIndexChanged.connect(self._refresh)
        form.addRow("Unité du fichier", self.unit)
        self.split = QCheckBox("Séparer les corps disjoints en pièces")
        self.split.setChecked(True)
        self.split.setToolTip("Un assemblage exporté en un seul STL donne une pièce par "
                              "volume fermé.")
        self.split.toggled.connect(self._refresh)
        form.addRow("", self.split)
        self.material = QComboBox()
        for name in sorted(SOLIDS, key=material_label):
            self.material.addItem(material_label(name), name)
        self.material.setCurrentIndex(max(0, self.material.findData("aluminium")))
        form.addRow("Matériau des pièces", self.material)
        lay.addLayout(form)

        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Pièce", "Taille (mm)", "Triangles", "Surface"])
        self.list.setRootIsDecorated(False)
        self.list.setMinimumHeight(160)
        lay.addWidget(self.list, 1)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        set_prop(self.summary, role="muted")
        lay.addWidget(self.summary)
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(f"color: {COLORS['error_text']}; font-size: 12px;")
        lay.addWidget(self.warning)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                       | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Importer")
        self.buttons.accepted.connect(self.accept_import)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._load()

    def _load(self) -> None:
        try:
            self._raw = cad.read(self.path, "m") if self.path.suffix.lower() in \
                cad.STL_SUFFIXES else cad.read(self.path)
        except (OSError, cad.CadError) as exc:
            self.error = str(exc)
            self._raw = []
        self._refresh()

    def _refresh(self) -> None:
        scale = 1.0
        unit = self.unit.currentData()
        if unit is not None:  # STL read in metres per unit above: rescale
            scale = cad.UNITS[unit]
        bodies = [cad.Body(b.name, b.triangles * scale) for b in self._raw]
        if self.split.isChecked():
            bodies = [p for b in bodies for p in cad.split_bodies(b)]
        self.bodies = bodies
        self.list.clear()
        open_parts = []
        for b in bodies:
            s = b.size
            holes = cad.open_edges(b.triangles)
            if holes:
                open_parts.append(b.name)
            QTreeWidgetItem(self.list, [b.name, " × ".join(_mm(v) for v in s),
                                        f"{len(b.triangles):,}".replace(",", " "),
                                        "ouverte" if holes else "fermée"])
        for i in range(4):
            self.list.resizeColumnToContents(i)
        if self.error:
            self.summary.setText("")
            self.warning.setText(f"Lecture impossible : {self.error}")
        else:
            if bodies:
                los, his = zip(*(b.bounds for b in bodies))
                size = [max(h[i] for h in his) - min(lo[i] for lo in los) for i in range(3)]
                self.summary.setText(f"{len(bodies)} pièce{'s' if len(bodies) > 1 else ''}, "
                                     f"encombrement {' × '.join(_mm(v) for v in size)} mm.")
            self.warning.setText(
                "Surface ouverte (" + ", ".join(open_parts[:4])
                + ("…" if len(open_parts) > 4 else "") + ") : son remplissage peut être "
                "faux, vérifiez la coupe du maillage dans l’onglet Volume d’air."
                if open_parts else "")
        self.warning.setVisible(bool(self.warning.text()))
        self.ok_button.setEnabled(bool(bodies) and not self.error)

    def accept_import(self) -> None:
        if not self.bodies or self.error:
            return
        self.result_import = CadImport(self.path, list(self.bodies),
                                       str(self.material.currentData()))
        self.accept()


def choose_cad_file(parent: QWidget | None = None) -> Path | None:
    path, _ = QFileDialog.getOpenFileName(parent, "Importer un fichier CAO", "", cad.FILE_FILTER)
    return Path(path) if path else None


def ask_cad_import(parent: QWidget | None = None, path: str | Path | None = None
                   ) -> CadImport | None:
    """Pick a file (unless ``path`` is given) and show the import dialog."""
    path = Path(path) if path else choose_cad_file(parent)
    if path is None:
        return None
    dlg = CadImportDialog(path, parent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.result_import
