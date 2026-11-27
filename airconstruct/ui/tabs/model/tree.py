"""Model tree of the Modèle tab: the enclosure and the parts, with groups, hidden
and locked parts. Synchronised with the shared selection.

Owned by: thread "Modèle" (see docs/ui-architecture.md). Built like the shared
``ObjectTree`` (same look), plus groups: clicking a group selects its parts.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem

from ...selection import ENCLOSURE, component_key, parse_key
from ...theme import COLORS
from ...view import PASSIVE_COLORS, _heat_color
from ...widgets.object_tree import swatch
from . import geometry as geo

KEY_ROLE = Qt.ItemDataRole.UserRole
GROUP_ROLE = Qt.ItemDataRole.UserRole + 1


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def part_color(c: dict, p_max: float) -> str:
    power = _num(c.get("power"))
    if power > 0:
        return _heat_color(power, p_max)
    return PASSIVE_COLORS.get(c.get("material"), "#9aa5b1")


class ModelTree(QTreeWidget):
    context_menu_requested = Signal(object)  # global position

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.setIndentation(14)
        self.setRootIsDecorated(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_menu_requested.emit(self.viewport().mapToGlobal(pos)))
        self._syncing = False
        self._items: dict[str, QTreeWidgetItem] = {}
        self.group_items: dict[str, QTreeWidgetItem] = {}
        self._expanded: dict[str, bool] = {}
        self.itemSelectionChanged.connect(self._tree_to_selection)
        self.itemExpanded.connect(lambda i: self._remember(i, True))
        self.itemCollapsed.connect(lambda i: self._remember(i, False))
        ctx.project.changed.connect(self.rebuild)
        ctx.selection.changed.connect(lambda _k: self._selection_to_tree())
        ctx.units.changed.connect(self.rebuild)
        self.rebuild()

    def _remember(self, item: QTreeWidgetItem, expanded: bool) -> None:
        g = item.data(0, GROUP_ROLE)
        if g:
            self._expanded[g] = expanded

    def _section(self, text: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self, [text.upper(), ""])
        f = item.font(0)
        f.setPixelSize(11)
        f.setWeight(QFont.Weight.DemiBold)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
        item.setFont(0, f)
        item.setForeground(0, QBrush(QColor(COLORS["muted"])))
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setExpanded(True)
        return item

    def _row(self, parent, text: str, detail: str, color: str | None, key: str | None,
             dim: bool = False) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [text, detail])
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(1, QBrush(QColor(COLORS["muted"])))
        f = item.font(0)
        f.setWeight(QFont.Weight.Medium)
        item.setFont(0, f)
        if dim:
            item.setForeground(0, QBrush(QColor("#9CA3AF")))
        if color:
            item.setIcon(0, swatch(color))
        if key:
            item.setData(0, KEY_ROLE, key)
        return item

    def rebuild(self) -> None:
        data = self.ctx.project.get() or {}
        u = self.ctx.units
        m = geo.read_meta(data)
        hidden, locked = set(m["hidden"]), set(m["locked"])
        self._syncing = True
        try:
            self.clear()
            self._items, self.group_items = {}, {}
            sec = self._section("Enceinte")
            e = data.get("enclosure") or {}
            try:
                size = " × ".join(f"{u.length_to_display(e[a]):g}" for a in "xyz")
            except (KeyError, TypeError):
                size = ""
            self._items[ENCLOSURE] = self._row(sec, "Boîte", size, COLORS["subtle"], ENCLOSURE)
            sec = self._section("Pièces")
            comps = data.get("components") or []
            p_max = max((_num(c.get("power")) for c in comps), default=0.0)
            placed: set[str] = set()
            for g, members in m["groups"].items():
                gi = self._row(sec, g, "groupe", None, None)
                gi.setData(0, GROUP_ROLE, g)
                gi.setIcon(0, swatch(COLORS["subtle"]))
                self.group_items[g] = gi
                for n in members:
                    c = geo.comp(data, n)
                    if c is not None:
                        self._part_row(gi, c, p_max, hidden, locked)
                        placed.add(n)
                gi.setExpanded(self._expanded.get(g, True))
            for c in comps:
                if str(c.get("name")) not in placed:
                    self._part_row(sec, c, p_max, hidden, locked)
            if not comps:
                hint = QTreeWidgetItem(sec, ["Aucune pièce : ajoutez-en une", ""])
                hint.setForeground(0, QBrush(QColor(COLORS["muted"])))
                hint.setFlags(Qt.ItemFlag.ItemIsEnabled)
        finally:
            self._syncing = False
        self._selection_to_tree()

    def _part_row(self, parent, c: dict, p_max: float, hidden: set, locked: set) -> None:
        name = str(c.get("name"))
        power = _num(c.get("power"))
        from .library import material_label

        detail = f"{power:g} W" if power else material_label(str(c.get("material", "")))
        flags = [t for t, on in (("masqué", name in hidden), ("verrouillé", name in locked)) if on]
        if flags:
            detail = " · ".join([*flags, detail])
        self._items[component_key(name)] = self._row(
            parent, name, detail, part_color(c, p_max), component_key(name), name in hidden)

    def _tree_to_selection(self) -> None:
        if self._syncing:
            return
        keys: list[str] = []
        for item in self.selectedItems():
            g = item.data(0, GROUP_ROLE)
            if g:
                data = self.ctx.project.get() or {}
                keys += [component_key(n) for n in geo.read_meta(data)["groups"].get(g, [])]
            elif item.data(0, KEY_ROLE):
                keys.append(item.data(0, KEY_ROLE))
        cur = self.currentItem().data(0, KEY_ROLE) if self.currentItem() else None
        keys = [k for k in keys if k != cur] + ([cur] if cur in keys else [])
        self.ctx.selection.set(keys)

    def _selection_to_tree(self) -> None:
        self._syncing = True
        try:
            keys = set(self.ctx.selection.keys)
            for key, item in self._items.items():
                item.setSelected(key in keys)
            data = self.ctx.project.get() or {}
            for g, item in self.group_items.items():
                members = {component_key(n) for n in geo.read_meta(data)["groups"].get(g, [])}
                item.setSelected(bool(members) and members <= keys)
            cur = self.ctx.selection.current
            if cur in self._items:
                self.scrollToItem(self._items[cur])
        finally:
            self._syncing = False

    def selected_parts(self) -> list[str]:
        return [n for k in self.ctx.selection.keys
                for kind, n in [parse_key(k)] if kind == "component" and n]
