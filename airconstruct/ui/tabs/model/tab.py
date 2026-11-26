"""Step 1, Modèle: enclosure and parts.

Owned by: thread "Modèle" (see docs/ui-architecture.md).

Foundation state: object tree on the left (shared selection), and on the right the
old case form as a TEMPORARY editor so the app stays usable until the Modèle, Air
and Volume d'air tabs have their own editors. Remove ``LegacyFormBinding`` and
``form.py`` once nothing uses them.
"""

from __future__ import annotations

from pydantic import ValidationError
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QScrollArea, QWidget

from ...form import CaseForm, FormError, format_duration
from ...theme import set_prop
from ...widgets.card import Card
from ...widgets.object_tree import ObjectTree
from ..base import StepTab


class LegacyFormBinding:
    """Two-way link between the old CaseForm and the project (one undo step per
    burst of typing)."""

    MERGE_KEY = "legacy-form"

    def __init__(self, ctx, form: CaseForm) -> None:
        self.ctx = ctx
        self.form = form
        self._pushing = False
        self._timer = QTimer(form)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.push)
        form.changed.connect(self._timer.start)
        ctx.project.changed.connect(self.pull)
        self.pull()

    def push(self) -> None:
        """Form -> project."""
        self._timer.stop()
        try:
            data = self.form.to_data(base=self.ctx.project.data)
        except FormError as exc:
            self.ctx.show_message(f"Valeur invalide : {exc}")
            return
        self._pushing = True
        try:
            self.ctx.project.set_data("Modifier le cas", data, merge_key=self.MERGE_KEY)
        finally:
            self._pushing = False

    def pull(self) -> None:
        """Project -> form (after undo, open, or an edit made by another tab)."""
        if self._pushing:
            return
        case = self.ctx.project.case
        if case is None:
            return
        try:
            same = self.form.to_case() == case
        except (FormError, ValidationError, ValueError):
            same = False
        if not same:
            self.form.set_case(case)
        self.update_estimate()

    def update_estimate(self) -> None:
        case = self.ctx.project.case
        if case is None:
            self.form.set_estimate("Temps estimé : –")
            return
        try:
            s = self.ctx.services.estimate_runtime(case)
        except NotImplementedError:
            self.form.set_estimate("Temps estimé : pas encore disponible")
            return
        except Exception as exc:  # noqa: BLE001
            self.form.set_estimate(f"Temps estimé : indisponible ({exc})")
            return
        self.form.set_estimate(f"Temps estimé : environ {format_duration(s)}")


class ModelTab(StepTab):
    key = "model"
    title = "Modèle"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.tree = ObjectTree(ctx)
        self.form = CaseForm()
        self.binding = LegacyFormBinding(ctx, self.form)
        ctx.runner.running_changed.connect(lambda running: self.form.setEnabled(not running))

        self._left = Card("Modèle", margins=(6, 6, 6, 6))
        self._left.body.addWidget(self.tree, 1)

        self._right = Card("Éditeur provisoire")
        rl = self._right.body
        note = QLabel("Sera remplacé par l’inspecteur de chaque étape.")
        note.setWordWrap(True)
        set_prop(note, role="muted")
        rl.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidget(self.form)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(440)
        rl.addWidget(scroll, 1)

    def left_panel(self) -> QWidget:
        return self._left

    def right_panel(self) -> QWidget:
        return self._right
