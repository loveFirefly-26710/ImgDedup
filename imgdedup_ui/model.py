"""界面数据层：分组行模型 + 缩略图异步缓存。

两个设计要点：

1. 虚拟化靠 Qt 原生 model/view，不是自己算可视区。
   `QListView` 只会向 model 索取可见行的数据，delegate 只画可见行，
   所以控件数与图库规模无关：10 万张和 1 千张的开销一样。

2. 缩略图绝不在 `paint()` 里加载。paint 里加载会直接卡住 UI。
   做法是：paint 时先查缓存，命中就画；未命中就画占位色块并发起异步加载，
   加载完通过信号让视图重绘那一行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import shiboken6
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

#: 缩略图在网格里的显示尺寸（像素）
THUMB_SIZE = 112

KEEP = "keep"
DELETE = "delete"
UNDECIDED = "undecided"


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
    """把 `ItemRow` 包装成 `group.recommend_keeper()` 认得的形状。

    `recommend_keeper` 的评分用到 `pixels / size / fmt / mtime_ns` 四个属性
    （见 `imgdedup_core/group.py`）。直接传 `ItemRow` 会缺 `pixels`，
    所以这里显式补上派生属性：也让 core 层不必知道 `ItemRow` 长什么样。
    """

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
    kind: str                       # exact | perceptual | semantic | cluster
    label: str                      # 「字节完全相同」「相似度 94%」
    items: list[ItemRow] = field(default_factory=list)
    #: 被用户（或自动推荐）指定「保留」的行内下标集合。
    #:
    #: 早期版本是单个 `keep: int`，语义上只允许保留一张。用户明确要求
    #: 「一组可以保留多张，数量不限」—— 同一个场景连拍的两张都不想删，
    #: 是真实需求。改集合后，`auto_decide()` 的语义仍然是
    #: 「保留集合之外的一律标删」，不变量「至少保留 1 张」由集合非空保证。
    keeps: set[int] = field(default_factory=set)
    oversized: bool = False         # 过合并护栏拦下的「聚类」

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def keep(self) -> int:
        """兼容字段：推荐/首个保留项的下标。

        只给「需要一个代表性的保留项」的地方用（右侧详情的推荐提示、
        旧的测试与报告）。判断某张是否被保留必须用 `is_kept(pos)`，
        否则多保留场景下会漏判。
        """
        return min(self.keeps) if self.keeps else 0

    def is_kept(self, pos: int) -> bool:
        return pos in self.keeps

    @property
    def deletable(self) -> bool:
        """超大聚类不允许批量删除：语义去重最危险的就是过合并。"""
        return not self.oversized

    @property
    def reclaim(self) -> int:
        return sum(i.size for p, i in enumerate(self.items)
                   if not self.is_kept(p) and i.decision == DELETE)

    def auto_decide(self) -> None:
        """默认裁决：保留集合内不动，其余标记待删。用户可逐项改。"""
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
    """把核心引擎的分组结果转成界面行。

    entries/groups 的契约与 `imgdedup_core.group` 一致：groups 是下标列表。
    """
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


# ================================================================ 行模型

class GroupModel(QObject):
    """QListView 的数据源。只暴露「行」，不做任何像素工作。"""

    changed = Signal()
    counts_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[GroupRow] = []
        self._visible: list[int] = []      # 过滤后的行下标

    # ---------------------------------------------------------- 数据
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
        """过滤后可见的行（顺序与视图一致）。

        方向键换行要用它判边界：用 `rows`（全量）会让光标跳过
        被筛掉的行，表现为「按一下↓跳了好远」。
        """
        return [self._rows[i] for i in self._visible]

    def refilter(self, kinds: set[str] | None = None) -> None:
        if kinds is None:
            self._visible = list(range(len(self._rows)))
        else:
            self._visible = [i for i, r in enumerate(self._rows) if r.kind in kinds]
        self.changed.emit()

    # ---------------------------------------------------------- 裁决
    def set_keep(self, visible_index: int, pos: int) -> None:
        """把某一张设为「保留」（替代原来的推荐保留项）。"""
        self.set_keeps(visible_index, {pos})

    def set_keeps(self, visible_index: int, positions: set[int]) -> None:
        """把一组位置整体设为保留集合：支持一次保留多张。

        `positions` 为空则忽略（不变量 1：每组至少保留 1 张）。
        """
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

    def add_keep(self, visible_index: int, positions: set[int]) -> None:
        """追加保留（不清掉已有的）—— 多选后「设为保留」按钮走这条。"""
        row = self.row_at(visible_index)
        if row is None:
            return
        valid = {p for p in positions if 0 <= p < row.count}
        if not valid:
            return
        row.keeps |= valid
        row.auto_decide()
        self.changed.emit()
        self.counts_changed.emit()

    def toggle_keeps(self, visible_index: int, positions: set[int]) -> None:
        """多选「设为保留」：已全是保留 → 取消（回退到推荐那张）；否则全部设保留。

        取消时不能把保留集合清空（不变量 1），所以退回到自动推荐的那张。
        推荐项本身不在被取消的范围内时也一并算进去，否则用户没法把
        「误设保留的某张」单独取消掉。
        """
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

    def toggle_delete(self, visible_index: int, pos: int) -> None:
        row = self.row_at(visible_index)
        if row is None or not (0 <= pos < row.count):
            return
        if row.is_kept(pos):
            return                       # 保留项不可标删（不变量 1）
        item = row.items[pos]
        item.decision = KEEP if item.decision == DELETE else DELETE
        self.changed.emit()
        self.counts_changed.emit()

    def toggle_delete_many(self, visible_index: int, positions: set[int]) -> None:
        """多选「标记删除 / 取消删除标记」。

        与单选版语义一致：保留项永远跳过（不变量 1），不因为
        「多选里混了一张保留的」就整批拒绝：那样用户完全不知道发生了什么。
        基准方向按「这批里是否已有待删」定，让整批结果一致而不是各自翻转。
        """
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
        """把某一张退回「未裁决」。右键菜单用。"""
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
        """简化版撤销：把所有裁决退回未决。"""
        touched = False
        for row in self._rows:
            if row.pending() != row.count:
                row.clear_decisions()
                touched = True
        self.changed.emit()
        self.counts_changed.emit()
        return touched

    # ---------------------------------------------------------- 统计
    def stats(self) -> dict:
        groups = len(self._rows)
        files = sum(r.count for r in self._rows)
        to_delete = sum(1 for r in self._rows for i in r.items if i.decision == DELETE)
        reclaim = sum(r.reclaim for r in self._rows)
        pending = sum(1 for r in self._rows if r.pending() == r.count)
        return {"groups": groups, "files": files, "to_delete": to_delete,
                "reclaim": reclaim, "pending_groups": pending}

    def planned(self) -> list[tuple[str, int]]:
        """待删清单 [(path, size)]，供路径 A 执行。"""
        out = []
        for row in self._rows:
            if not row.deletable:
                continue
            for p, item in enumerate(row.items):
                if item.decision == DELETE and p not in row.keeps:
                    out.append((item.path, item.size))
        return out


def make_list_model(store: "GroupModel"):
    """把纯数据的 `GroupModel` 适配成 Qt 的 `QAbstractListModel`。

    为什么要分两层：`GroupModel` 是纯 Python 的，可以在没有 Qt 事件循环的
    环境里被单测（模型层不该被 GUI 框架绑死）；Qt 适配只做最薄的一层 ——
    转发 rowCount/data，并把「整表变化」映射成 beginResetModel/endResetModel。

    虚拟化靠的就是这层：QListView 只对可见行调 data()，
    所以 10 万行和 10 行的开销一样。
    """
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


# ================================================================ 缩略图

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
            # 延迟导入：核心引擎会拉起 numpy/pyvips，不能在主线程启动时加载
            from imgdedup_core import decode as D

            arr, _backend = D.load_small(self.path, self.size)
        except Exception:  # noqa: BLE001
            arr = None

        if arr is None:
            self.cache._on_loaded(self.token, None)
            return

        try:
            h, w, ch = arr.shape
            img = QImage(arr.data, w, h, ch * w, QImage.Format_RGB888).copy()
            self.cache._on_loaded(self.token, QPixmap.fromImage(img))
        except Exception:  # noqa: BLE001
            self.cache._on_loaded(self.token, None)


class ThumbCache(QObject):
    """异步缩略图缓存。

    `paint()` 里只能调 `get()`；未命中就 `request()` 并在回调里重绘。
    """

    loaded = Signal(int)          # token

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
        """请求异步加载。返回 token（未发起则为 None）。

        缓存 key 必须带上 `size`：预览框（1024px）和网格（112px）是
        两个不同尺寸的 ThumbCache 实例，各自一份缓存，不会串。
        但同一实例内如果 size 变了（旧代码只有一个全局 cache），
        会读到尺寸不对的图，所以 key 里带 size 是防御性的。
        """
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
        """后台任务回填结果。

        这是从工作线程直接调用的（`_ThumbTask.run`），不是槽函数。
        窗口先关、任务后完成时，本对象已被 Qt 销毁，
        `self.loaded.emit()` 会抛 `RuntimeError: Signal source has been deleted`
       ：不崩，但会往 stderr 刷一大片噪音，掩盖真正的失败。
        `closeEvent` 里的 `shutdown()` 只能缩短这个窗口，收不掉全部。
        所以这里用 `shiboken6.isValid` 显式挡一道。
        """
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
        """拆掉后台线程池，等正在跑的缩略图任务收尾。

        必须显式调用：`ThumbCache` 是 `MainWindow` 的子对象，
        父对象析构时 Qt 先把它和信号一起销毁，而线程池里的任务还在跑，
        回来 `loaded.emit()` 就会撞上「Signal source has been deleted」。
        这本身不会崩（Qt 会打印、异常被吞在事件循环里），但会污染测试输出，
        并且在窗口关闭的瞬间有机会真崩。窗口 `closeEvent` 里调用它。
        """
        self._pool.clear()          # 扔掉还没排上队的任务
        self._pool.waitForDone(3000)
