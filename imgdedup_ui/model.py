"""界面侧的数据模型：分组行、决策状态与缩略图缓存。

删除决策（保留 / 删除 / 未裁决）只活在这里，`planned()` 是交给删除动作的出口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import shiboken6
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

THUMB_SIZE = 112

KEEP = "keep"
DELETE = "delete"
UNDECIDED = "undecided"

KIND_ORDER = {"exact": 0, "perceptual": 1, "semantic": 2, "cluster": 3}


@dataclass
class ItemRow:
    path: str
    size: int
    width: int = 0
    height: int = 0
    fmt: str = ""
    decision: str = UNDECIDED
    note: str = ""

    @property
    def dims(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "尺寸未知"


class _FakeEntry:

    __slots__ = ("width", "height", "size", "fmt", "pixels", "mtime_ns")

    def __init__(self, item: ItemRow):
        self.width = item.width
        self.height = item.height
        self.size = item.size
        self.fmt = item.fmt
        self.pixels = item.width * item.height
        self.mtime_ns = 0

    @classmethod
    def of(cls, row: "GroupRow") -> list["_FakeEntry"]:
        return [cls(i) for i in row.items]


@dataclass
class GroupRow:
    kind: str
    label: str
    items: list[ItemRow] = field(default_factory=list)
    keeps: set[int] = field(default_factory=set)
    oversized: bool = False

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def keep(self) -> int:
        return min(self.keeps) if self.keeps else 0

    def is_kept(self, pos: int) -> bool:
        return pos in self.keeps

    @property
    def deletable(self) -> bool:
        return not self.oversized

    @property
    def reclaim(self) -> int:
        return sum(i.size for p, i in enumerate(self.items)
                   if not self.is_kept(p) and i.decision == DELETE)

    def auto_decide(self) -> None:
        if not self.keeps:
            self.keeps = {0}
        for p, item in enumerate(self.items):
            item.decision = KEEP if self.is_kept(p) else DELETE

    def clear_decisions(self) -> None:
        for item in self.items:
            item.decision = UNDECIDED

    def pending(self) -> int:
        return sum(1 for i in self.items if i.decision == UNDECIDED)


def build_rows(entries, groups, kind: str, *, label_of: Callable | None = None,
               oversized: bool = False) -> list[GroupRow]:
    from imgdedup_core import group as G

    rows: list[GroupRow] = []
    for g in groups:
        if len(g) < 2:
            continue
        keep = G.recommend_keeper(entries, g)
        items = [
            ItemRow(path=entries[i].path, size=entries[i].size,
                    width=entries[i].width, height=entries[i].height,
                    fmt=entries[i].fmt or "")
            for i in g
        ]
        try:
            keep_pos = g.index(keep)
        except ValueError:
            keep_pos = 0
        label = label_of(entries, g, keep) if label_of else kind
        row = GroupRow(kind=kind, label=label, items=items, keeps={keep_pos},
                       oversized=oversized)
        row.auto_decide()
        rows.append(row)
    return rows


def merged_label(kind: str, n_units: int, n_free: int) -> str:
    if n_units >= 2 or (n_units == 1 and n_free):
        return f"含 {n_units} 组字节重复"
    return kind


def build_merged_rows(entries, components, exact_groups, *,
                      label_of: Callable[[str, int, int], str] | None = None
                      ) -> list[GroupRow]:
    from imgdedup_core import group as G

    unit_of: dict[int, int] = {}
    for u, g in enumerate(exact_groups):
        for i in g:
            unit_of[i] = u

    rows: list[GroupRow] = []
    for kind, members in sorted(components,
                                key=lambda km: (KIND_ORDER.get(km[0], 9),
                                                km[1][0])):
        if len(members) < 2:
            continue
        pos = {idx: k for k, idx in enumerate(members)}
        units = sorted({unit_of[i] for i in members if i in unit_of})
        free = [i for i in members if i not in unit_of]

        keeps = {pos[G.recommend_keeper(entries, members)]}

        items = [
            ItemRow(path=entries[i].path, size=entries[i].size,
                    width=entries[i].width, height=entries[i].height,
                    fmt=entries[i].fmt or "")
            for i in members
        ]
        label = (label_of(kind, len(units), len(free)) if label_of
                 else merged_label(kind, len(units), len(free)))
        row = GroupRow(kind=kind, label=label, items=items, keeps=keeps)
        row.auto_decide()
        rows.append(row)
    return rows



class GroupModel(QObject):

    changed = Signal()
    counts_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[GroupRow] = []
        self._visible: list[int] = []

    def set_rows(self, rows: list[GroupRow]) -> None:
        self._rows = rows
        self.refilter()
        self.changed.emit()
        self.counts_changed.emit()

    @property
    def rows(self) -> list[GroupRow]:
        return self._rows

    def row_at(self, visible_index: int) -> Optional[GroupRow]:
        if 0 <= visible_index < len(self._visible):
            return self._rows[self._visible[visible_index]]
        return None

    def visible_count(self) -> int:
        return len(self._visible)

    def visible_rows(self) -> list[GroupRow]:
        return [self._rows[i] for i in self._visible]

    def refilter(self, kinds: set[str] | None = None) -> None:
        if kinds is None:
            self._visible = list(range(len(self._rows)))
        else:
            self._visible = [i for i, r in enumerate(self._rows) if r.kind in kinds]
        self.changed.emit()

    def set_keep(self, visible_index: int, pos: int) -> None:
        self.set_keeps(visible_index, {pos})

    def set_keeps(self, visible_index: int, positions: set[int]) -> None:
        row = self.row_at(visible_index)
        if row is None:
            return
        valid = {p for p in positions if 0 <= p < row.count}
        if not valid:
            return
        row.keeps = valid
        row.auto_decide()
        self.changed.emit()
        self.counts_changed.emit()

    def toggle_keeps(self, visible_index: int, positions: set[int]) -> None:
        row = self.row_at(visible_index)
        if row is None:
            return
        valid = {p for p in positions if 0 <= p < row.count}
        if not valid:
            return
        if valid <= row.keeps:
            remaining = row.keeps - valid
            if not remaining:
                from imgdedup_core import group as G

                entries = _FakeEntry.of(row)
                remaining = {G.recommend_keeper(entries, list(range(row.count)))}
            row.keeps = remaining
        else:
            row.keeps |= valid
        row.auto_decide()
        self.changed.emit()
        self.counts_changed.emit()

    def toggle_delete_many(self, visible_index: int, positions: set[int]) -> None:
        row = self.row_at(visible_index)
        if row is None:
            return
        valid = {p for p in positions
                 if 0 <= p < row.count and not row.is_kept(p)}
        if not valid:
            return
        any_delete = any(row.items[p].decision == DELETE for p in valid)
        target = DELETE if not any_delete else KEEP
        for p in valid:
            row.items[p].decision = target
        self.changed.emit()
        self.counts_changed.emit()

    def set_undecided(self, visible_index: int, pos: int) -> None:
        row = self.row_at(visible_index)
        if row is None or not (0 <= pos < row.count) or row.is_kept(pos):
            return
        row.items[pos].decision = UNDECIDED
        self.changed.emit()
        self.counts_changed.emit()

    def apply_auto_to_all(self) -> None:
        for row in self._rows:
            if row.deletable:
                row.auto_decide()
        self.changed.emit()
        self.counts_changed.emit()

    def undo_last(self) -> bool:
        touched = False
        for row in self._rows:
            if row.pending() != row.count:
                row.clear_decisions()
                touched = True
        self.changed.emit()
        self.counts_changed.emit()
        return touched

    def stats(self) -> dict:
        groups = len(self._rows)
        files = sum(r.count for r in self._rows)
        to_delete = sum(1 for r in self._rows for i in r.items if i.decision == DELETE)
        reclaim = sum(r.reclaim for r in self._rows)
        pending = sum(1 for r in self._rows if r.pending() == r.count)
        return {"groups": groups, "files": files, "to_delete": to_delete,
                "reclaim": reclaim, "pending_groups": pending}

    def planned(self) -> list[tuple[str, int]]:
        out = []
        for row in self._rows:
            if not row.deletable:
                continue
            for p, item in enumerate(row.items):
                if item.decision == DELETE and p not in row.keeps:
                    out.append((item.path, item.size))
        return out


def make_list_model(store: "GroupModel"):
    from PySide6.QtCore import QAbstractListModel, QModelIndex

    class _GroupListModel(QAbstractListModel):
        def __init__(self, inner: GroupModel, parent=None):
            super().__init__(parent)
            self.inner = inner
            inner.changed.connect(self._on_changed)

        def _on_changed(self) -> None:
            self.beginResetModel()
            self.endResetModel()

        def rowCount(self, parent=QModelIndex()) -> int:
            return 0 if parent.isValid() else self.inner.visible_count()

        def data(self, index, role=Qt.DisplayRole):
            if not index.isValid() or role != Qt.UserRole:
                return None
            return self.inner.row_at(index.row())

    return _GroupListModel(store)



class _ThumbTask(QRunnable):
    def __init__(self, cache: "ThumbCache", path: str, size: int, token: int):
        super().__init__()
        self.cache = cache
        self.path = path
        self.size = size
        self.token = token
        self.setAutoDelete(True)

    def run(self) -> None:
        arr = None
        try:
            from imgdedup_core import decode as D

            arr, _backend = D.load_small(self.path, self.size)
        except Exception:
            arr = None

        if arr is None:
            self.cache._on_loaded(self.token, None)
            return

        try:
            h, w, ch = arr.shape
            img = QImage(arr.data, w, h, ch * w, QImage.Format_RGB888).copy()
            self.cache._on_loaded(self.token, QPixmap.fromImage(img))
        except Exception:
            self.cache._on_loaded(self.token, None)


class ThumbCache(QObject):

    loaded = Signal(int)

    def __init__(self, size: int = THUMB_SIZE, capacity: int = 800, parent=None):
        super().__init__(parent)
        self.size = size
        self.capacity = capacity
        self._cache: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._token_path: dict[int, str] = {}
        self._next_token = 1
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max(2, min(6, (QThreadPool.globalInstance()
                                                    .maxThreadCount() or 4))))

    def get(self, path: str) -> Optional[QPixmap]:
        return self._cache.get((path, self.size))

    def request(self, path: str) -> Optional[int]:
        key = (path, self.size)
        if key in self._cache or key in self._pending:
            return None
        self._pending.add(key)
        token = self._next_token
        self._next_token += 1
        self._token_path[token] = key
        self._pool.start(_ThumbTask(self, path, self.size, token))
        return token

    def _on_loaded(self, token: int, pixmap: Optional[QPixmap]) -> None:
        if not shiboken6.isValid(self):
            return
        key = self._token_path.pop(token, None)
        if key is None:
            return
        self._pending.discard(key)
        if pixmap is not None:
            if len(self._cache) >= self.capacity:
                for k in list(self._cache.keys())[: self.capacity // 4]:
                    self._cache.pop(k, None)
            self._cache[key] = pixmap
        self.loaded.emit(token)

    def clear(self) -> None:
        self._cache.clear()
        self._pending.clear()
        self._token_path.clear()

    def wait(self, msec: int = 3000) -> None:
        self._pool.waitForDone(msec)
        self._pool.clear()

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(3000)
