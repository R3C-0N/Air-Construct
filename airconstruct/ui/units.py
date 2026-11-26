"""Display units (lengths in mm or m, temperatures in °C or K), chosen in the status bar.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

The project data stays in SI (metres, °C as in the Case model). Widgets convert with
:meth:`Units.length_to_display` / :meth:`Units.length_from_display` and format with
:meth:`Units.fmt_length` / :meth:`Units.fmt_temperature`, and redraw on ``changed``.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal

KELVIN = 273.15
LENGTHS = {"mm": 1e-3, "m": 1.0}
TEMPERATURES = {"C": "°C", "K": "K"}


def _fr(v: float, digits: int) -> str:
    return f"{v:.{digits}f}".replace(".", ",")


class Units(QObject):
    changed = Signal()

    def __init__(self, settings: QSettings | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        length = str(settings.value("units/length", "mm")) if settings else "mm"
        temp = str(settings.value("units/temperature", "C")) if settings else "C"
        self._length = length if length in LENGTHS else "mm"
        self._temperature = temp if temp in TEMPERATURES else "C"

    # ------------------------------------------------------------------ choice
    @property
    def length(self) -> str:
        return self._length

    @property
    def temperature(self) -> str:
        return self._temperature

    def set_length(self, unit: str) -> None:
        if unit not in LENGTHS:
            raise ValueError(unit)
        if unit != self._length:
            self._length = unit
            if self._settings:
                self._settings.setValue("units/length", unit)
            self.changed.emit()

    def set_temperature(self, unit: str) -> None:
        if unit not in TEMPERATURES:
            raise ValueError(unit)
        if unit != self._temperature:
            self._temperature = unit
            if self._settings:
                self._settings.setValue("units/temperature", unit)
            self.changed.emit()

    @property
    def label(self) -> str:
        """Status bar text, e.g. ``"mm · °C"``."""
        return f"{self._length} · {TEMPERATURES[self._temperature]}"

    @property
    def length_symbol(self) -> str:
        return self._length

    @property
    def temperature_symbol(self) -> str:
        return TEMPERATURES[self._temperature]

    # ------------------------------------------------------------------ conversion
    def length_to_display(self, metres: float) -> float:
        return metres / LENGTHS[self._length]

    def length_from_display(self, value: float) -> float:
        return value * LENGTHS[self._length]

    def temperature_to_display(self, celsius: float) -> float:
        return celsius + KELVIN if self._temperature == "K" else celsius

    def temperature_from_display(self, value: float) -> float:
        return value - KELVIN if self._temperature == "K" else value

    def fmt_length(self, metres: float, digits: int | None = None) -> str:
        d = (1 if self._length == "mm" else 4) if digits is None else digits
        return f"{_fr(self.length_to_display(metres), d)} {self._length}"

    def fmt_temperature(self, celsius: float, digits: int = 1) -> str:
        return f"{_fr(self.temperature_to_display(celsius), digits)} {self.temperature_symbol}"
