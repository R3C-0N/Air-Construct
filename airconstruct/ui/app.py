"""Main window: the app shell around the five step tabs.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

Layout (see the "Studio" mockup): a top bar (Fichier menu, project name and saved
state, the step tabs with alert badges, undo/redo, expert mode), then the active
tab's left panel, the shared 3D viewport and the tab's right panel, and the status
bar (alerts, messages, units, shortcuts). The shell owns the shared state
(:class:`~airconstruct.ui.context.AppContext`); the tabs own their panels.

Start it with ``airconstruct-gui [case.yaml] [--setup-openfoam]`` or ``python -m airconstruct.ui``
(``--setup-openfoam``: offer to install WSL2 / OpenFOAM if missing, see foam_setup).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import cad
from ..case import Case
from ..foam_setup import RESUME_FLAG
from .alerts import Alert, AlertCenter
from .context import AppContext, ExpertMode, Services
from .project import Project
from .run import RunController
from .selection import Selection
from .statusbar import StatusBar
from .tabs import TAB_CLASSES, StepTab
from .theme import COLORS, apply_theme, ensure_theme, set_prop
from .units import Units
from .view import geometry_items
from .viewport import Viewport
from .widgets.card import Card, CardColumn
from .widgets.step_nav import StepNav
from .widgets.toggle import ToggleSwitch

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "electronics_box.yaml"
MAX_RECENT = 8
OPEN_FILTER = f"Cas Air-Construct (*.yaml *.yml);;{cad.FILE_FILTER}"


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


def _default_preview(case: Case) -> dict[str, float]:
    from ..thermal_estimate import estimate_temperatures

    return estimate_temperatures(case)


def _default_write_report(case: Case, case_dir: Path, out_dir: Path, summary=None) -> Path:
    from ..results import write_report

    return write_report(case, case_dir, out_dir, summary=summary)


#: steps whose content the model lock (a run going on) freezes; shown in their tooltip
LOCKED_WHILE_RUNNING = ("model", "air", "volume")


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
        preview_fn: Callable[[Case], dict[str, float]] | None = None,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        ensure_theme()
        self.settings = settings or QSettings("AirConstruct", "AirConstruct")
        self.work_dir = Path(
            work_dir or self.settings.value("work_dir", str(Path.home() / "AirConstruct" / "runs"))
        )
        start = case or default_case()
        self.project = Project(start.model_dump(mode="json"), case_path, parent=self)
        self.selection = Selection(self)
        self.units = Units(self.settings, self)
        self.alerts = AlertCenter(self.project, self)
        self.expert = ExpertMode(self.settings, self)
        self.runner = RunController(self.project, lambda: self.work_dir, run_fn, summarize_fn,
                                    load_regions_fn, parent=self)
        self.viewport = Viewport(self.selection)
        self.ctx = AppContext(
            project=self.project, selection=self.selection, units=self.units,
            alerts=self.alerts, viewport=self.viewport, runner=self.runner,
            expert=self.expert, settings=self.settings,
            services=Services(estimate_runtime=estimate_fn or _default_estimate,
                              quick_temperatures=preview_fn or _default_preview,
                              write_report=write_report_fn or _default_write_report,
                              open_report=open_report),
            goto_tab=self.goto_tab, show_message=self.show_message,
            open_project=self.load_case,
            work_dir=lambda: self.work_dir,
        )
        self.confirm_discard: Callable[[], bool] = self._ask_save_changes
        from .tabs.model.cad_dialog import ask_cad_import
        from .tabs.model.new_project import ask_new_project

        self.ask_new_project = ask_new_project
        self.ask_cad_import = ask_cad_import

        self.tabs: dict[str, StepTab] = {}
        self.tab_order: list[str] = []
        self.current_tab: StepTab | None = None

        self.resize(1440, 900)
        self._scene_timer = QTimer(self)
        self._scene_timer.setSingleShot(True)
        self._scene_timer.setInterval(30)
        self._scene_timer.timeout.connect(self.refresh_scene)
        self._build_actions()
        self._build_ui()
        for cls in TAB_CLASSES:
            self.add_tab(cls(self.ctx))

        self.project.changed.connect(self._on_project_changed)
        self.project.edit_refused.connect(self.show_message)
        self.project.lock_changed.connect(lambda _l: self._update_badges())
        self.project.replaced.connect(self._on_project_replaced)
        self.project.dirty_changed.connect(lambda _d: self._update_title())
        self.project.path_changed.connect(self._update_title)
        self.project.results_changed.connect(self._scene_timer.start)
        self.alerts.changed.connect(self._update_badges)
        self.runner.running_changed.connect(lambda _r: self._update_run_action())
        self.alerts.changed.connect(self._update_run_action)

        self.goto_tab(self.tab_order[0])
        self.alerts.refresh()
        self._update_badges()
        self._update_title()
        self._update_run_action()
        self.refresh_scene(reset_camera=True)

    # ------------------------------------------------------------------ layout
    def _action(self, text: str, slot, keys=(), parent_menu: QMenu | None = None) -> QAction:
        a = QAction(text, self)
        if keys:
            a.setShortcuts([QKeySequence(k) for k in keys])
        a.triggered.connect(slot)
        self.addAction(a)  # shortcuts work without a menu bar
        if parent_menu is not None:
            parent_menu.addAction(a)
        self.global_actions.append(a)
        return a

    def _build_actions(self) -> None:
        self.global_actions: list[QAction] = []
        self.file_menu = QMenu("Fichier", self)
        m = self.file_menu
        self._action("&Nouveau…", self.new_project, [QKeySequence.StandardKey.New], m)
        self._action("&Ouvrir…", self.open_case, [QKeySequence.StandardKey.Open], m)
        self.recent_menu = m.addMenu("Récents")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        m.addSeparator()
        self._action("&Enregistrer", self.save_case, [QKeySequence.StandardKey.Save], m)
        self._action("Enregistrer &sous…", self.save_case_as, ["Ctrl+Shift+S"], m)
        m.addSeparator()
        self._action("Dossier des &calculs…", self.choose_work_dir, (), m)
        m.addSeparator()
        self._action("&Quitter", self.close, [QKeySequence.StandardKey.Quit], m)

        # undo/redo follow the stack, and are off while a run locks the model
        stack = self.project.undo_stack
        self.undo_action = QAction("Annuler", self)
        self.undo_action.setShortcuts([QKeySequence(QKeySequence.StandardKey.Undo)])
        self.undo_action.triggered.connect(
            lambda: None if self.project.locked else stack.undo())
        self.redo_action = QAction("Rétablir", self)
        self.redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.redo_action.triggered.connect(
            lambda: None if self.project.locked else stack.redo())
        for a in (self.undo_action, self.redo_action):
            self.addAction(a)
            self.global_actions.append(a)
        for sig in (stack.canUndoChanged, stack.canRedoChanged, stack.undoTextChanged,
                    stack.redoTextChanged, self.project.lock_changed):
            sig.connect(self._sync_undo_actions)
        self._sync_undo_actions()
        self.run_action = self._action("Lancer le calcul", self.start_run, ["Ctrl+R"])
        self._action("Tout désélectionner", self.selection.clear, ["Esc"])

    def _sync_undo_actions(self, *_args) -> None:
        stack, locked = self.project.undo_stack, self.project.locked
        for action, can, word, text in (
                (self.undo_action, stack.canUndo(), "Annuler", stack.undoText()),
                (self.redo_action, stack.canRedo(), "Rétablir", stack.redoText())):
            action.setEnabled(can and not locked)
            action.setText(f"{word} {text}".strip() if not locked
                           else f"{word} (verrouillé pendant le calcul)")

    def _action_button(self, action: QAction, glyph: str) -> QToolButton:
        """A fixed-glyph button that follows an action (its tooltip names the step)."""
        b = QToolButton()
        b.setText(glyph)
        b.setStyleSheet("font-size: 17px; min-width: 32px; min-height: 36px;")

        def sync() -> None:
            try:
                b.setEnabled(action.isEnabled())
                b.setToolTip(action.text())
            except RuntimeError:  # window being destroyed
                pass

        action.changed.connect(sync)
        b.clicked.connect(action.trigger)
        sync()
        return b

    @staticmethod
    def _bar_card() -> tuple[QFrame, QHBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(4, 4, 8, 4)
        lay.setSpacing(4)
        return card, lay

    def _build_ui(self) -> None:
        # top bar: three floating cards (file | steps | undo, expert), as in the mockup
        file_card, fl = self._bar_card()
        self.file_button = QToolButton()
        self.file_button.setText("Fichier ▾")
        self.file_button.setStyleSheet("font-size: 14px; min-height: 36px; padding: 0 12px;")
        self.file_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.file_button.setMenu(self.file_menu)
        fl.addWidget(self.file_button)
        sep = QFrame()
        sep.setFixedSize(1, 24)
        sep.setStyleSheet(f"background: {COLORS['input_border']};")
        fl.addWidget(sep)
        self.name_label = QLabel()
        set_prop(self.name_label, role="title")
        self.name_label.setContentsMargins(8, 0, 4, 0)
        fl.addWidget(self.name_label)
        self.state_label = QLabel()
        set_prop(self.state_label, role="pill")
        fl.addWidget(self.state_label)

        self.tab_bar = StepNav()
        self.tab_bar.currentChanged.connect(self._on_tab_bar_changed)

        tools_card, tl = self._bar_card()
        self.undo_button = self._action_button(self.undo_action, "↶")
        self.redo_button = self._action_button(self.redo_action, "↷")
        tl.addWidget(self.undo_button)
        tl.addWidget(self.redo_button)
        self.expert_check = ToggleSwitch("Mode expert")
        self.expert_check.setChecked(self.expert.on)
        self.expert_check.toggled.connect(self.expert.set)
        self.expert.changed.connect(self.expert_check.setChecked)
        tl.addSpacing(8)
        tl.addWidget(self.expert_check)

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(file_card)
        top.addStretch(1)
        top.addWidget(self.tab_bar)
        top.addStretch(1)
        top.addWidget(tools_card)

        # body: tab panels (cards) on each side of the shared viewport
        self.left_stack = QStackedWidget()
        self.left_stack.setMinimumWidth(260)
        self.right_stack = QStackedWidget()
        self.right_stack.setMinimumWidth(300)
        self.body = QSplitter(Qt.Orientation.Horizontal)
        self.body.setHandleWidth(12)
        self.body.setChildrenCollapsible(False)
        self.body.addWidget(self.left_stack)
        self.body.addWidget(self.viewport)
        self.body.addWidget(self.right_stack)
        self.body.setStretchFactor(1, 1)
        self.body.setSizes([280, 800, 340])

        # bottom: camera presets on the left, status card on the right
        self.status = StatusBar(self.alerts, self.units, self._on_alert_clicked,
                                self.all_shortcut_actions)
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addWidget(self.viewport.camera_card)
        bottom.addStretch(1)
        bottom.addWidget(self.status)

        central = QWidget()
        central.setObjectName("shell")
        cl = QVBoxLayout(central)
        cl.setContentsMargins(16, 16, 16, 16)
        cl.setSpacing(12)
        cl.addLayout(top)
        cl.addWidget(self.body, 1)
        cl.addLayout(bottom)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------ tabs
    def add_tab(self, tab: StepTab) -> None:
        self.tabs[tab.key] = tab
        self.tab_order.append(tab.key)
        tab._panels = {}
        for side, stack, w in (("left", self.left_stack, tab.left_panel()),
                               ("right", self.right_stack, tab.right_panel())):
            if w is not None and not isinstance(w, (Card, CardColumn)) and not w.property("cards"):
                card = Card()  # every side panel sits on a card
                card.body.addWidget(w)
                w = card
            if w is not None:
                stack.addWidget(w)
            tab._panels[side] = w
        self.tab_bar.addTab(tab.title)
        for a in tab.actions():
            a.setEnabled(False)
            self.addAction(a)
        n = len(self.tab_order)
        if n <= 9:
            self._action(f"Étape {n} : {tab.title}", lambda _=False, k=tab.key: self.goto_tab(k),
                         [f"Ctrl+{n}"])
        tab.scene_changed.connect(lambda t=tab: t.active and self._scene_timer.start())
        self.alerts.register(tab.alerts)

    def goto_tab(self, key: str) -> None:
        if key in self.tab_order:
            self.tab_bar.setCurrentIndex(self.tab_order.index(key))

    def _on_tab_bar_changed(self, index: int) -> None:
        if not 0 <= index < len(self.tab_order):
            return
        tab = self.tabs[self.tab_order[index]]
        old = self.current_tab
        if old is tab:
            return
        if old is not None:
            old.active = False
            for a in old.actions():
                a.setEnabled(False)
            old.deactivated()
        self.current_tab = tab
        tab.active = True
        for side, stack in (("left", self.left_stack), ("right", self.right_stack)):
            w = tab._panels.get(side)
            stack.setVisible(w is not None)
            if w is not None:
                stack.setCurrentWidget(w)
        self.viewport.set_tab_toolbar(tab.viewport_toolbar())
        for a in tab.actions():
            a.setEnabled(True)
        tab.activated()
        self.refresh_scene()

    def _update_badges(self) -> None:
        for i, key in enumerate(self.tab_order):
            alerts = self.alerts.for_tab(key)
            level = "error" if any(a.level == "error" for a in alerts) else "warning"
            lines = [a.message for a in alerts]
            if self.project.locked and key in LOCKED_WHILE_RUNNING:
                lines.insert(0, "Verrouillé pendant le calcul")
            self.tab_bar.buttons[i].set_badge(len(alerts), level, "\n".join(lines))

    def tab_badge(self, key: str) -> str:
        """Text of a step's alert badge ('' when none)."""
        return self.tab_bar.buttons[self.tab_order.index(key)].badge.text()

    def all_shortcut_actions(self) -> list[QAction]:
        out = list(self.global_actions)
        for tab in self.tabs.values():
            out.extend(tab.actions())
        return out

    def _on_alert_clicked(self, alert: Alert) -> None:
        if alert.tab:
            self.goto_tab(alert.tab)
        if alert.target:
            self.selection.set([alert.target])

    # ------------------------------------------------------------------ scene
    def default_scene(self) -> list:
        case = self.project.last_valid_case
        if case is None:
            return []
        try:
            return geometry_items(case)
        except ImportError:
            return []

    def refresh_scene(self, reset_camera: bool = False) -> None:
        self._scene_timer.stop()
        items = self.current_tab.scene() if self.current_tab is not None else None
        self.viewport.show_items(self.default_scene() if items is None else items, reset_camera)

    def _on_project_changed(self) -> None:
        self.selection.prune(self.project.get() or {})
        self._scene_timer.start()

    def _on_project_replaced(self) -> None:
        self.selection.clear()
        self.refresh_scene(reset_camera=True)

    def show_message(self, text: str, timeout_ms: int = 8000) -> None:
        self.status.showMessage(text, timeout_ms)

    def _update_title(self) -> None:
        p = self.project
        name = p.path.name if p.path else p.name
        self.name_label.setText(name)
        if p.dirty:
            state = "modifié"
        elif p.path is None:
            state = "non enregistré"
        else:
            state = "enregistré"
        self.state_label.setText(state)
        set_prop(self.state_label, tone="warning" if p.dirty else
                 "success" if p.path is not None else "")
        self.setWindowTitle(f"Air-Construct · {name}{' •' if p.dirty else ''}")

    # ------------------------------------------------------------------ run
    def _update_run_action(self) -> None:
        self.run_action.setEnabled(not self.runner.running and self.project.case is not None
                                   and not self.alerts.has_errors)

    def start_run(self) -> bool:
        if not self.run_action.isEnabled():
            return False
        return self.runner.start()

    # ------------------------------------------------------------------ files
    def _ask_save_changes(self) -> bool:
        """True when it is fine to drop the current project (saved, or the user agreed)."""
        if not self.project.dirty:
            return True
        ans = QMessageBox.question(
            self, "Modifications non enregistrées",
            "Le projet a été modifié. Enregistrer les modifications ?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if ans == QMessageBox.StandardButton.Save:
            return self.save_case()
        return ans == QMessageBox.StandardButton.Discard

    def new_case(self, case: Case | dict | None = None) -> bool:
        """Start a new project from ``case`` (a Case or project data, possibly not
        valid yet), default: the example."""
        if self.runner.running or not self.confirm_discard():
            return False
        case = case or default_case()
        self.project.reset(case.model_dump(mode="json") if isinstance(case, Case) else case)
        self.goto_tab(self.tab_order[0])
        return True

    def new_project(self) -> bool:
        """Fichier > Nouveau: the "Nouveau projet" dialog (templates, open, recent).
        ``ask_new_project`` is replaceable, for tests."""
        if self.runner.running:
            return False
        choice = self.ask_new_project(self, default_case().model_dump(mode="json"),
                                      self.recent_files())
        if choice is None:
            return False
        if isinstance(choice, (str, Path)):
            return self.load_case(Path(choice))
        return self.new_case(choice)

    def load_case(self, path: Path) -> bool:
        if Path(path).suffix.lower() in cad.SUFFIXES:
            return self.new_project_from_cad(Path(path))
        if self.runner.running or not self.confirm_discard():
            return False
        try:
            self.project.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Ouvrir", f"Impossible d’ouvrir {path} :\n{exc}")
            return False
        self._add_recent(Path(path))
        self.goto_tab(self.tab_order[0])
        return True

    def new_project_from_cad(self, path: Path) -> bool:
        """A CAD file opened (Fichier > Ouvrir, command line): a new, unsaved project
        built around its parts, after the import dialog (``ask_cad_import`` is
        replaceable, for tests)."""
        from .tabs.model.cadparts import new_project

        if self.runner.running:
            return False
        choice = self.ask_cad_import(self, path)
        if choice is None:
            return False
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(path).stem).strip("_-")
        name = stem if re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", stem) else "projet_cao"
        return self.new_case(new_project(choice.bodies, name, choice.source, choice.material))

    def open_case(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir un cas", "", OPEN_FILTER)
        if path:
            self.load_case(Path(path))

    def save_case(self) -> bool:
        if self.project.path is None:
            return self.save_case_as()
        return self.write_case(self.project.path)

    def save_case_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Enregistrer le cas", "",
                                              "Cas Air-Construct (*.yaml)")
        if not path:
            return False
        if not path.lower().endswith((".yaml", ".yml")):
            path += ".yaml"
        return self.write_case(Path(path))

    def write_case(self, path: Path) -> bool:
        try:
            saved = self.project.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Enregistrer", f"Impossible d’enregistrer :\n{exc}")
            return False
        self._add_recent(saved)
        msg = f"Enregistré dans {saved}"
        if self.project.case is None:
            msg += " (le cas n’est pas encore valide)"
        self.show_message(msg)
        return True

    def recent_files(self) -> list[str]:
        v = self.settings.value("recent_files", [])
        if isinstance(v, str):
            v = [v]
        return [str(p) for p in (v or [])]

    def _add_recent(self, path: Path) -> None:
        p = str(Path(path).resolve())
        files = [p] + [f for f in self.recent_files() if f != p]
        self.settings.setValue("recent_files", files[:MAX_RECENT])

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        files = self.recent_files()
        if not files:
            self.recent_menu.addAction("(aucun)").setEnabled(False)
        for f in files:
            a = self.recent_menu.addAction(f)
            a.triggered.connect(lambda _=False, p=f: self.load_case(Path(p)))

    def choose_work_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Dossier des calculs", str(self.work_dir))
        if d:
            self.work_dir = Path(d)
            self.settings.setValue("work_dir", d)
            self.show_message(f"Les calculs sont enregistrés dans {self.work_dir}")

    def offer_openfoam_setup(self):
        """Go to the Simulation step and offer to install what is missing."""
        sim = self.tabs.get("simulation")
        if sim is None:
            return None
        self.goto_tab("simulation")
        return sim.offer_setup()

    def closeEvent(self, event) -> None:
        if self.runner.running:
            ans = QMessageBox.question(self, "Quitter",
                                       "Un calcul est en cours. L’arrêter et quitter ?")
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.runner.stop()
            self.runner.wait(10000)
        elif not self.confirm_discard():
            event.ignore()
            return
        for tab in self.tabs.values():
            tab.closing()
        self.viewport.close_plotter()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("Air-Construct")
    apply_theme(app)
    setup = RESUME_FLAG in argv[1:]
    args = [a for a in argv[1:] if a != RESUME_FLAG]
    case_path = Path(args[0]) if args else None
    win = MainWindow()
    if case_path is not None:
        win.load_case(case_path)
    win.show()
    if setup:  # installer or restart: finish installing WSL2 / OpenFOAM
        QTimer.singleShot(300, win.offer_openfoam_setup)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
