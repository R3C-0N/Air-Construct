"""Look of the app, taken from the "Studio" mockup: colours, font, one Qt style sheet.

Owned by: workstream "ui" (shared core, see docs/ui-architecture.md).

Tabs do not write colours or style sheets of their own: they use the tokens of
:data:`COLORS` (``theme.COLORS["accent"]``) and the object names / properties the
style sheet knows:

- ``Card`` (widgets/card.py): white rounded panel; ``card.title``
- ``QLabel {{ background: transparent; }}
QLabel[role="section"]``: small uppercase section title ("PLACEMENT")
- ``QLabel[role="muted"]``: grey secondary text
- ``QLabel[role="pill"]`` with ``[tone="warning"|"error"|"success"|"info"]``: small badge
- ``QPushButton[kind="primary"]`` (blue), ``[kind="danger"]`` (red text),
  ``[kind="flat"]`` (no border), default: white bordered button
- ``QToolButton[kind="segment"]``: buttons of a segmented bar (Dessus/Face/Côté/Iso),
  checked = selected
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

FONT_DIR = Path(__file__).parent / "fonts"
FONT_FAMILY = "Inter"

COLORS = {
    "text": "#111827",
    "text_secondary": "#374151",
    "muted": "#4B5563",
    "subtle": "#6B7280",
    "background": "#E7EAEF",  # window ground, behind the cards
    "background_top": "#FFFFFF",  # top of the 3D view gradient
    "card": "#FFFFFF",
    "card_border": "#E2E5EA",
    "divider": "#EEF0F3",
    "input_border": "#D1D5DB",
    "field": "#F5F7FA",
    "hover": "#F3F4F6",
    "accent": "#1D4ED8",
    "accent_dark": "#1E3A8A",
    "accent_soft": "#EFF6FF",
    "error": "#E0445A",
    "error_text": "#B91C1C",
    "warning": "#F59E0B",
    "warning_text": "#B45309",
    "warning_soft": "#FEF3C7",
    "warning_soft_text": "#92400E",
    "success": "#166534",
    "success_soft": "#DCFCE7",
    "heat": "#F97316",
}

RADIUS_CARD = 14
RADIUS_CONTROL = 10


def load_fonts() -> str:
    """Register the bundled Inter font; returns the family to use."""
    found = False
    for f in sorted(FONT_DIR.glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(f)) >= 0:
            found = True
    return FONT_FAMILY if found else ""


def style_sheet(c: dict[str, str] = COLORS) -> str:
    return f"""
QMainWindow, QDialog {{ background: {c["background"]}; }}
QWidget {{ color: {c["text"]}; font-size: 13px; }}
QToolTip {{ background: {c["text"]}; color: white; border: 0; padding: 4px 6px;
           border-radius: 6px; }}

/* cards */
QFrame#card {{ background: {c["card"]}; border: 1px solid {c["card_border"]};
              border-radius: {RADIUS_CARD}px; }}
QFrame#card QLabel:!enabled {{ color: #9CA3AF; }}
QLabel#cardTitle {{ font-weight: 600; font-size: 13px; }}
QFrame#cardHeader {{ border: 0; border-bottom: 1px solid {c["divider"]};
                    background: transparent; }}
QWidget#cardBody {{ background: transparent; }}
QWidget#transparent {{ background: transparent; }}

/* text roles */
QLabel[role="section"] {{ color: {c["muted"]}; font-size: 11px; font-weight: 600;
                          letter-spacing: 0.6px; }}
QLabel[role="muted"] {{ color: {c["muted"]}; font-size: 12px; }}
QLabel[role="title"] {{ font-weight: 600; font-size: 14px; }}
QLabel[role="pill"] {{ border-radius: 9px; padding: 0 8px; max-height: 20px; min-height: 20px; font-size: 11px;
                       font-weight: 600; background: {c["hover"]}; color: {c["muted"]}; }}
QLabel[role="pill"][tone="warning"] {{ background: {c["warning_soft"]};
                                        color: {c["warning_soft_text"]}; }}
QLabel[role="pill"][tone="error"] {{ background: {c["error"]}; color: white; }}
QLabel[role="pill"][tone="success"] {{ background: {c["success_soft"]}; color: {c["success"]}; }}
QLabel[role="pill"][tone="info"] {{ background: {c["accent_soft"]}; color: {c["accent"]}; }}

/* buttons */
QPushButton {{ background: white; color: {c["text"]}; border: 1px solid {c["input_border"]};
              border-radius: {RADIUS_CONTROL}px; padding: 0 14px; min-height: 34px;
              font-weight: 600; }}
QPushButton:hover {{ background: {c["hover"]}; }}
QPushButton:pressed, QPushButton:checked {{ background: {c["accent_soft"]};
                                            border-color: {c["accent"]}; }}
QPushButton:disabled {{ color: #9CA3AF; background: #F9FAFB; border-color: #E5E7EB; }}
QPushButton[kind="primary"] {{ background: {c["accent"]}; color: white; border: 0; }}
QPushButton[kind="primary"]:hover {{ background: {c["accent_dark"]}; }}
QPushButton[kind="primary"]:disabled {{ background: #93A8E8; color: white; }}
QPushButton[kind="danger"] {{ color: {c["error_text"]}; }}
QPushButton[kind="flat"] {{ border: 0; background: transparent; }}
QPushButton[kind="flat"]:hover {{ background: {c["hover"]}; }}
QToolButton {{ border: 0; background: transparent; border-radius: 8px; padding: 4px 10px;
              min-height: 28px; font-weight: 600; color: {c["text_secondary"]}; }}
QToolButton:hover {{ background: {c["hover"]}; }}
QToolButton:disabled {{ color: #C0C4CC; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton[kind="segment"] {{ font-size: 12px; min-height: 32px; }}
QToolButton[kind="segment"]:checked {{ background: {c["accent_soft"]}; color: {c["accent"]}; }}

/* step navigation (top bar) */
QFrame#stepNav {{ background: {c["card"]}; border: 1px solid {c["card_border"]};
                 border-radius: 22px; }}
QPushButton#step {{ border: 0; background: transparent; border-radius: 17px; min-height: 34px;
                   padding: 0 14px; font-weight: 500; color: {c["text_secondary"]}; }}
QPushButton#step:hover {{ background: {c["hover"]}; }}
QPushButton#step:checked {{ background: {c["accent"]}; color: white; font-weight: 600; }}
QLabel#stepBadge {{ border-radius: 9px; font-size: 11px; font-weight: 700; padding: 0 5px; }}

/* inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: white;
    border: 1px solid {c["input_border"]}; border-radius: 8px; padding: 0 8px;
    min-height: 32px; selection-background-color: {c["accent"]}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {c["accent"]}; }}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QComboBox QAbstractItemView {{ background: white; border: 1px solid {c["card_border"]};
    selection-background-color: {c["accent_soft"]}; selection-color: {c["accent_dark"]}; }}
QCheckBox {{ spacing: 8px; }}

/* lists, trees, tables */
QTreeView, QListView, QTableView, QTableWidget, QTreeWidget {{ background: transparent;
    border: 0; outline: none; alternate-background-color: {c["field"]}; }}
QTreeView {{ show-decoration-selected: 1; }}
QTreeView::item, QListView::item {{ min-height: 30px; padding: 0 4px; border: 0; }}
QTreeView::item:hover, QListView::item:hover {{ background: {c["hover"]}; }}
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{
    background: {c["accent_soft"]}; color: {c["accent_dark"]}; }}
QTableView {{ gridline-color: {c["divider"]}; }}
QHeaderView::section {{ background: transparent; border: 0;
    border-bottom: 1px solid {c["divider"]}; padding: 4px 6px; font-size: 12px;
    font-weight: 600; color: {c["muted"]}; }}
QPlainTextEdit, QTextEdit {{ background: {c["field"]}; border: 0; border-radius: 8px;
    font-family: "JetBrains Mono", "Consolas", "DejaVu Sans Mono", monospace; font-size: 12px; }}

/* misc */
QProgressBar {{ background: {c["hover"]}; border: 0; border-radius: 6px; min-height: 22px;
               text-align: center; font-size: 12px; color: {c["text"]}; }}
QProgressBar::chunk {{ background: #BFDBFE; border-radius: 6px; }}
QGroupBox {{ border: 1px solid {c["divider"]}; border-radius: 10px; margin-top: 16px;
            padding-top: 6px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
QScrollArea {{ background: transparent; border: 0; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #CBD0D8; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #CBD0D8; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QSplitter::handle {{ background: transparent; }}
QMenu {{ background: white; border: 1px solid {c["card_border"]}; border-radius: 10px;
        padding: 4px; }}
QMenu::item {{ padding: 6px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c["accent_soft"]}; color: {c["accent_dark"]}; }}
QMenu::separator {{ height: 1px; background: {c["divider"]}; margin: 4px 6px; }}
QSlider::groove:horizontal {{ height: 4px; background: {c["input_border"]}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {c["accent"]}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; border: 2px solid {c["accent"]};
    width: 14px; height: 14px; margin: -7px 0; border-radius: 9px; }}
"""


def apply_theme(app: QApplication) -> None:
    """Font, palette and style sheet for the whole application (call once)."""
    family = load_fonts()
    app.setStyle("Fusion")  # same base look on Windows and Linux; the sheet does the rest
    font = QFont(family or app.font().family())
    font.setPixelSize(13)
    app.setFont(font)
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor(COLORS["background"]))
    pal.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["accent"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(pal)
    app.setStyleSheet(style_sheet())


def ensure_theme() -> None:
    """Apply the theme once per application (the window calls it)."""
    app = QApplication.instance()
    if app is not None and not app.property("airconstruct_theme"):
        apply_theme(app)
        app.setProperty("airconstruct_theme", True)


def set_prop(widget, **props) -> None:
    """Set style properties (``kind="primary"``, ``role="muted"``...) and re-polish."""
    for k, v in props.items():
        widget.setProperty(k, v)
    st = widget.style()
    st.unpolish(widget)
    st.polish(widget)
