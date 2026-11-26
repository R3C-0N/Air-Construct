"""Bottom bar: alert count (click to go to the object at fault), messages, units,
keyboard shortcuts.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QMenu,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .alerts import ERROR, WARNING, Alert, AlertCenter
from .units import LENGTHS, TEMPERATURES, Units

LEVEL_ICON = {ERROR: "✖", WARNING: "▲", "info": "ℹ"}


def alerts_text(n: int) -> str:
    if n == 0:
        return "✓ Aucune alerte"
    return f"{n} alerte" + ("s" if n > 1 else "")


class StatusBar(QStatusBar):
    def __init__(self, alerts: AlertCenter, units: Units,
                 on_alert: Callable[[Alert], None],
                 shortcuts: Callable[[], list[QAction]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.alert_center = alerts
        self.units = units
        self._on_alert = on_alert
        self._shortcuts = shortcuts

        self.alerts_button = QToolButton()
        self.alerts_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.alerts_menu = QMenu(self.alerts_button)
        self.alerts_button.setMenu(self.alerts_menu)
        self.addWidget(self.alerts_button)

        self.units_button = QToolButton()
        self.units_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.units_button)
        self.length_group = QActionGroup(self)
        for unit in LENGTHS:
            a = menu.addAction(f"Longueurs en {unit}")
            a.setCheckable(True)
            a.setData(unit)
            self.length_group.addAction(a)
            a.triggered.connect(lambda _=False, u=unit: units.set_length(u))
        menu.addSeparator()
        self.temperature_group = QActionGroup(self)
        for unit, symbol in TEMPERATURES.items():
            a = menu.addAction(f"Températures en {symbol}")
            a.setCheckable(True)
            a.setData(unit)
            self.temperature_group.addAction(a)
            a.triggered.connect(lambda _=False, u=unit: units.set_temperature(u))
        self.units_button.setMenu(menu)
        self.addPermanentWidget(self.units_button)

        self.shortcuts_button = QToolButton()
        self.shortcuts_button.setText("Raccourcis  ?")
        self.shortcuts_button.clicked.connect(self.show_shortcuts)
        self.addPermanentWidget(self.shortcuts_button)

        alerts.changed.connect(self._update_alerts)
        units.changed.connect(self._update_units)
        self._update_alerts()
        self._update_units()

    def _update_alerts(self) -> None:
        items = self.alert_center.alerts
        self.alerts_button.setText(alerts_text(len(items)))
        color = ("#B91C1C" if any(a.level == ERROR for a in items)
                 else "#B45309" if items else "#15803D")
        self.alerts_button.setStyleSheet(f"color: {color};")
        self.alerts_menu.clear()
        for a in items:
            act = self.alerts_menu.addAction(f"{LEVEL_ICON.get(a.level, '')}  {a.message}")
            act.triggered.connect(lambda _=False, alert=a: self._on_alert(alert))
        self.alerts_button.setEnabled(bool(items))

    def _update_units(self) -> None:
        self.units_button.setText(f"{self.units.label} ▾")
        for a in self.length_group.actions():
            a.setChecked(a.data() == self.units.length)
        for a in self.temperature_group.actions():
            a.setChecked(a.data() == self.units.temperature)

    def shortcut_rows(self) -> list[tuple[str, str]]:
        rows = []
        for a in self._shortcuts():
            keys = ", ".join(s.toString() for s in a.shortcuts())
            if keys:
                rows.append((a.text().replace("&", "").rstrip("…"), keys))
        return rows

    def show_shortcuts(self) -> QDialog:
        dlg = QDialog(self.window())
        dlg.setWindowTitle("Raccourcis clavier")
        lay = QVBoxLayout(dlg)
        rows = self.shortcut_rows()
        table = QTableWidget(len(rows), 2)
        table.setHorizontalHeaderLabels(["Action", "Raccourci"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, (text, keys) in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(text))
            table.setItem(r, 1, QTableWidgetItem(keys))
        table.resizeColumnsToContents()
        lay.addWidget(table)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(dlg.reject)
        lay.addWidget(box)
        dlg.resize(420, 420)
        dlg.show()
        return dlg
