"""Object tree (Enceinte, Pièces, Ouvertures) synchronised with the shared selection.

Owned by: workstream "ui" (shared widget, see docs/ui-architecture.md). Tabs create
their own instance (``ObjectTree(ctx, kinds=...)``); every instance follows the
same :class:`~airconstruct.ui.selection.Selection`, so a click in the tree selects
in the 3D view and the other way round.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem

from ..selection import ENCLOSURE, component_key, opening_key

KEY_ROLE = Qt.ItemDataRole.UserRole
KINDS = ("enclosure", "component", "opening")


class ObjectTree(QTreeWidget):
    def __init__(self, ctx, kinds: tuple[str, ...] = KINDS, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.kinds = kinds
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._syncing = False
        self._items: dict[str, QTreeWidgetItem] = {}
        self.itemSelectionChanged.connect(self._tree_to_selection)
        ctx.project.changed.connect(self.rebuild)
        ctx.selection.changed.connect(lambda _k: self._selection_to_tree())
        ctx.units.changed.connect(self.rebuild)
        self.rebuild()

    def _add(self, parent, text: str, key: str | None, detail: str = "") -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [f"{text}   {detail}".rstrip()])
        if key is not None:
            item.setData(0, KEY_ROLE, key)
            self._items[key] = item
        else:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
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
                self._add(self, "Enceinte", ENCLOSURE, size)
            if "component" in self.kinds:
                group = self._add(self, "Pièces", None)
                for c in data.get("components") or []:
                    power = c.get("power") or 0
                    detail = f"{c.get('material', '')}" + (f" · {power:g} W" if power else "")
                    self._add(group, str(c.get("name")), component_key(str(c.get("name"))),
                              detail)
                group.setExpanded(True)
            if "opening" in self.kinds:
                group = self._add(self, "Ouvertures", None)
                for o in data.get("openings") or []:
                    kind = {"inlet": "entrée", "outlet": "sortie"}.get(o.get("kind"), "")
                    self._add(group, str(o.get("name")), opening_key(str(o.get("name"))),
                              f"{kind} · {o.get('face', '')}")
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
