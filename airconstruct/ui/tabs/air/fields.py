"""Small form helpers of the Air tab's inspectors (spin boxes, segmented bars, rows).

Owned by: thread "Air".
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QWidget,
)

from ...theme import set_prop


def spin(lo: float, hi: float, decimals: int = 1, suffix: str = "", step: float = 1.0,
         tip: str = "") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setMinimumWidth(56)  # do not size to the widest value: the panels are narrow
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setKeyboardTracking(False)  # one edit when the user finishes typing
    if suffix:
        s.setSuffix(f" {suffix}")
    if tip:
        s.setToolTip(tip)
    return s


def set_value(widget, value) -> None:
    """Set a widget's value without emitting its change signal."""
    widget.blockSignals(True)
    try:
        if isinstance(widget, QDoubleSpinBox):
            if abs(widget.value() - float(value)) > 10 ** (-widget.decimals()) / 2:
                widget.setValue(float(value))
        elif isinstance(widget, QComboBox):
            i = widget.findData(value)
            if i >= 0:
                widget.setCurrentIndex(i)
        elif hasattr(widget, "setChecked"):
            widget.setChecked(bool(value))
        elif hasattr(widget, "setText"):
            if widget.text() != str(value):
                widget.setText(str(value))
        elif hasattr(widget, "setValue"):
            widget.setValue(value)
    finally:
        widget.blockSignals(False)


def combo(items: dict) -> QComboBox:
    c = QComboBox()
    narrow(c)
    for key, label in items.items():
        c.addItem(label, key)
    return c


def narrow(c: QComboBox) -> QComboBox:
    """Let a combo box be narrower than its longest item."""
    c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    c.setMinimumContentsLength(8)
    return c


def form() -> QFormLayout:
    f = QFormLayout()
    f.setContentsMargins(0, 0, 0, 0)
    f.setHorizontalSpacing(10)
    f.setVerticalSpacing(6)
    f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return f


def section(text: str) -> QLabel:
    lab = QLabel(text.upper())
    set_prop(lab, role="section")
    return lab


def muted(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    set_prop(lab, role="muted")
    return lab


def pair(a: QWidget, b: QWidget) -> QWidget:
    w = QWidget()
    w.setObjectName("transparent")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lay.addWidget(a, 1)
    lay.addWidget(b, 1)
    return w


class Segmented(QFrame):
    """A row of checkable segment buttons; ``changed(key)`` when the user picks one."""

    changed = Signal(str)

    def __init__(self, items: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QToolButton] = {}
        for key, label in items.items():
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            set_prop(b, kind="segment")
            b.clicked.connect(lambda _=False, k=key: self.changed.emit(k))
            self.group.addButton(b)
            lay.addWidget(b)
            self.buttons[key] = b
        lay.addStretch(1)

    def set_current(self, key: str) -> None:
        b = self.buttons.get(key)
        if b is not None:
            b.setChecked(True)

    @property
    def current(self) -> str | None:
        return next((k for k, b in self.buttons.items() if b.isChecked()), None)
