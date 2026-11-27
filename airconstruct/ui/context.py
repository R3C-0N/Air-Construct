"""Everything a step tab can use, handed to each tab at construction.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from ..case import Case
from .alerts import AlertCenter
from .project import Project
from .run import RunController
from .selection import Selection
from .units import Units
from .viewport import Viewport


class ExpertMode(QObject):
    """The "Mode expert" switch of the top bar: tabs show advanced settings when on."""

    changed = Signal(bool)

    def __init__(self, settings: QSettings | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._on = bool(settings.value("expert", False, type=bool)) if settings else False

    @property
    def on(self) -> bool:
        return self._on

    def set(self, on: bool) -> None:
        if bool(on) != self._on:
            self._on = bool(on)
            if self._settings:
                self._settings.setValue("expert", self._on)
            self.changed.emit(self._on)


@dataclass
class Services:
    """Slow or optional computations, injectable for tests."""

    estimate_runtime: Callable[[Case], float]  # seconds
    quick_temperatures: Callable[[Case], dict[str, float]]  # {component: °C}
    write_report: Callable  # (case, case_dir, out_dir, summary=None) -> Path
    open_report: bool = True


@dataclass
class AppContext:
    project: Project
    selection: Selection
    units: Units
    alerts: AlertCenter
    viewport: Viewport
    runner: RunController
    expert: ExpertMode
    settings: QSettings
    services: Services
    goto_tab: Callable[[str], None] = field(default=lambda key: None)
    show_message: Callable[[str], None] = field(default=lambda text: None)
    work_dir: Callable[[], Path] = field(default=lambda: Path.home() / "AirConstruct" / "runs")
    #: open another project file (asks to save the current one); True when opened
    open_project: Callable[[Path], bool] = field(default=lambda path: False)
