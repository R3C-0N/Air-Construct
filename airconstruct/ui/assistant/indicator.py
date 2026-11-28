"""The "Claude" pill of the top bar: shows what Claude is doing, lets the user pause it.

Owned by: workstream "assistant".

Hidden until Claude connects. Then it reads "Claude connecté"; while a command runs
and for a few seconds after, "Claude · <action>" on the accent colour; "Claude en
pause" when the user paused it. Its menu lists the last actions and holds the pause
and switch-off entries.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMenu, QToolButton

from ..theme import set_prop
from .bridge import Activity, AssistantBridge

ACTIVE_MS = 4000  # how long an action stays shown on the pill
MENU_ACTIONS = 15
MAX_TEXT = 48


class AssistantIndicator(QToolButton):
    def __init__(self, bridge: AssistantBridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.setObjectName("assistant")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_ = QMenu(self)
        self.menu_.aboutToShow.connect(self._fill_menu)
        self.setMenu(self.menu_)
        self._last: Activity | None = None
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(ACTIVE_MS)
        self._idle.timeout.connect(self._refresh)
        bridge.activity.connect(self._on_activity)
        bridge.clients_changed.connect(lambda _n: self._refresh())
        bridge.paused_changed.connect(lambda _p: self._refresh())
        bridge.enabled_changed.connect(lambda _e: self._refresh())
        self._refresh()

    @property
    def state(self) -> str:
        """hidden | idle | active | paused | error"""
        b = self.bridge
        if not b.enabled:
            return "hidden"
        if b.paused:
            return "paused"
        if b.client_count == 0 and not b.history:
            return "hidden"
        if self._idle.isActive() and self._last is not None:
            return "active" if self._last.ok else "error"
        return "idle"

    def _on_activity(self, act: Activity) -> None:
        self._last = act
        self._idle.start()
        self._refresh()

    def _refresh(self) -> None:
        state = self.state
        self.setVisible(state != "hidden")
        if state == "paused":
            text = "✦ Claude en pause"
        elif state in ("active", "error") and self._last is not None:
            t = self._last.text
            text = f"✦ Claude · {t if len(t) <= MAX_TEXT else t[:MAX_TEXT - 1] + '…'}"
        elif self.bridge.client_count:
            text = "✦ Claude connecté"
        else:
            text = "✦ Claude"
        self.setText(text)
        tip = "Claude pilote Air-Construct. Cliquez pour voir ses actions, le mettre en pause."
        if self._last is not None and not self._last.ok:
            tip += f"\nDernier refus : {self._last.error}"
        self.setToolTip(tip)
        set_prop(self, state=state)

    def _fill_menu(self) -> None:
        m = self.menu_
        m.clear()
        b = self.bridge
        if b.history:
            title = m.addAction("Dernières actions de Claude")
            title.setEnabled(False)
            for act in reversed(b.history[-MENU_ACTIONS:]):
                when = time.strftime("%H:%M:%S", time.localtime(act.when))
                mark = "" if act.ok else "  ✗ refusé"
                a = m.addAction(f"{when}   {act.text}{mark}")
                if not act.ok:
                    a.setToolTip(act.error)
            m.addSeparator()
        pause = m.addAction("Reprendre" if b.paused else "Mettre Claude en pause")
        pause.triggered.connect(lambda: b.set_paused(not b.paused))
