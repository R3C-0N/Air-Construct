"""Object tree (Enceinte, Pièces, Ouvertures) synchronised with the shared selection.

Owned by: workstream "ui" (shared widget, see docs/ui-architecture.md). Tabs create
their own instance (``ObjectTree(ctx, kinds=...)``); every instance follows the
same :class:`~airconstruct.ui.selection.Selection`, so a click in the tree selects
in the 3D view and the other way round.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem

from ..selection import ENCLOSURE, component_key, opening_key
from ..theme import COLORS
from ..view import PASSIVE_COLORS, _heat_color

KEY_ROLE = Qt.ItemDataRole.UserRole
KINDS = ("enclosure", "component", "opening")


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def swatch(color: str, size: int = 10) -> QIcon:
    """Small rounded colour square, as the dots of the mockup's tree."""
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), 3, 3)
    p.end()
    return QIcon(pm)


class ObjectTree(QTreeWidget):
    def __init__(self, ctx, kinds: tuple[str, ...] = KINDS, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.kinds = kinds
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.setIndentation(14)
        self.setRootIsDecorated(False)
        self.setIconSize(self.iconSize().scaled(10, 10, Qt.AspectRatioMode.KeepAspectRatio))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # no dotted focus frame
        self._syncing = False
        self._items: dict[str, QTreeWidgetItem] = {}
        self.itemSelectionChanged.connect(self._tree_to_selection)
        ctx.project.changed.connect(self.rebuild)
        ctx.selection.changed.connect(lambda _k: self._selection_to_tree())
        ctx.units.changed.connect(self.rebuild)
        self.rebuild()

    def _add(self, parent, text: str, key: str | None, detail: str = "",
             color: str | None = None) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [text, detail])
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(1, QBrush(QColor(COLORS["muted"])))
        if key is not None:
            item.setData(0, KEY_ROLE, key)
            f = item.font(0)
            f.setWeight(QFont.Weight.Medium)
            item.setFont(0, f)
            if color:
                item.setIcon(0, swatch(color))
            self._items[key] = item
        else:  # section title: small uppercase grey text
            item.setText(0, text.upper())
            f = item.font(0)
            f.setPixelSize(11)
            f.setWeight(QFont.Weight.DemiBold)
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
            item.setFont(0, f)
            item.setForeground(0, QBrush(QColor(COLORS["muted"])))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return item

    def rebuild(self) -> None:
        data = self.ctx.project.get() or {}
        u = self.ctx.units
        self._syncing = True
        try:
            self.clear()
            self._items = {}
            if "enclosure" in self.kinds:
                e = data.get("enclosure") or {}
                try:
                    size = " × ".join(f"{u.length_to_display(e[a]):g}" for a in "xyz")
                    size += f" {u.length_symbol}"
                except (KeyError, TypeError):
                    size = ""
                group = self._add(self, "Enceinte", None)
                self._add(group, "Boîte", ENCLOSURE, size, COLORS["subtle"])
                group.setExpanded(True)
            if "component" in self.kinds:
                group = self._add(self, "Pièces", None)
                comps = data.get("components") or []
                p_max = max((_num(c.get("power")) for c in comps), default=0.0)
                for c in comps:
                    power = _num(c.get("power"))
                    detail = f"{power:g} W" if power else str(c.get("material", ""))
                    color = (_heat_color(power, p_max) if power > 0
                             else PASSIVE_COLORS.get(c.get("material"), "#9aa5b1"))
                    self._add(group, str(c.get("name")), component_key(str(c.get("name"))),
                              detail, color)
                group.setExpanded(True)
            if "opening" in self.kinds:
                group = self._add(self, "Ouvertures", None)
                for o in data.get("openings") or []:
                    kind = {"inlet": "entrée", "outlet": "sortie"}.get(o.get("kind"), "")
                    color = "#2f80ed" if o.get("kind") == "inlet" else "#27ae60"
                    self._add(group, str(o.get("name")), opening_key(str(o.get("name"))),
                              f"{kind} · {o.get('face', '')}", color)
                group.setExpanded(True)
        finally:
            self._syncing = False
        self._selection_to_tree()

    def _tree_to_selection(self) -> None:
        if self._syncing:
            return
        keys = [i.data(0, KEY_ROLE) for i in self.selectedItems()]
        current = self.currentItem().data(0, KEY_ROLE) if self.currentItem() else None
        keys = [k for k in keys if k and k != current] + ([current] if current in keys else [])
        self.ctx.selection.set(keys)

    def _selection_to_tree(self) -> None:
        self._syncing = True
        try:
            keys = set(self.ctx.selection.keys)
            for key, item in self._items.items():
                item.setSelected(key in keys)
            cur = self.ctx.selection.current
            if cur in self._items:
                self.scrollToItem(self._items[cur])
        finally:
            self._syncing = False
