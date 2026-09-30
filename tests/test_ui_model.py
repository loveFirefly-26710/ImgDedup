"""界面层的逻辑测试。

刻意把「模型层」做成不依赖 Qt 的纯 Python（`imgdedup_ui.model.GroupModel`），
就是为了让这些断言能在没有事件循环的环境里跑。

这里不测像素：像素问题靠离屏截图看（`.scratch/verify_ui.py`），
比如"进度条百分比压到下一段标签上"那种 bug，纯逻辑测不出来。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView  # noqa: E402

from imgdedup_core.scan import FileEntry  # noqa: E402
from imgdedup_ui.model import (DELETE, KEEP, UNDECIDED, GroupModel, GroupRow,  # noqa: E402
                               ItemRow, build_rows)

def ent(path, size=1000, w=800, h=600, fmt="PNG", mtime=0):
    return FileEntry(path=path, size=size, mtime_ns=mtime, ext=".png",
                     width=w, height=h, fmt=fmt)


class TestBuildRows(unittest.TestCase):
    def setUp(self):
        self.entries = [
            ent("a1.png", size=1000, w=800, h=600),
            ent("a2.png", size=900, w=800, h=600),
            ent("a3.png", size=800, w=640, h=480),
            ent("b1.png", size=500, w=100, h=100),
            ent("b2.png", size=400, w=100, h=100),
        ]

    def test_keep_is_the_best_one(self):
        rows = build_rows(self.entries, [[0, 1, 2]], "exact")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].keep, 0, "分辨率/体积最优的应当被推荐保留")

    def test_auto_decide_marks_the_rest_delete(self):
        rows = build_rows(self.entries, [[0, 1, 2]], "exact")
        decisions = [i.decision for i in rows[0].items]
        self.assertEqual(decisions, [KEEP, DELETE, DELETE])

    def test_single_member_group_is_dropped(self):
        self.assertEqual(build_rows(self.entries, [[3]], "exact"), [])

    def test_oversized_flag_blocks_batch_delete(self):
        rows = build_rows(self.entries, [[0, 1, 2]], "cluster", oversized=True)
        self.assertTrue(rows[0].oversized)
        self.assertFalse(rows[0].deletable)

    def test_group_row_reclaim_only_counts_delete(self):
        row = GroupRow(kind="exact", label="x",
                       items=[ItemRow("a", 100), ItemRow("b", 200), ItemRow("c", 300)],
                       keeps={0})
        row.auto_decide()
        self.assertEqual(row.reclaim, 500)
        row.items[1].decision = UNDECIDED
        self.assertEqual(row.reclaim, 300)


class TestGroupModel(unittest.TestCase):
    def setUp(self):
        self.entries = [ent(f"g{i}.png", size=1000 - i, w=800, h=600) for i in range(3)]
        self.entries += [ent(f"h{i}.png", size=500 - i, w=200, h=200) for i in range(2)]
        self.model = GroupModel()
        self.model.set_rows(build_rows(self.entries, [[0, 1, 2], [3, 4]], "exact"))

    def test_row_count_and_lookup(self):
        self.assertEqual(self.model.visible_count(), 2)
        self.assertEqual(self.model.row_at(0).count, 3)
        self.assertIsNone(self.model.row_at(9))

    def test_cannot_mark_the_keeper_for_deletion(self):
        """不变量 1：保留项不可被标删。"""
        row = self.model.row_at(0)
        keep_pos = row.keep
        self.model.toggle_delete(0, keep_pos)
        self.assertEqual(row.items[keep_pos].decision, KEEP,
                         "保留项被标成了待删 —— 违反了每组至少留 1 张")

    def test_toggle_flips_and_back(self):
        before = self.model.stats()["to_delete"]
        self.model.toggle_delete(0, 1)
        self.assertEqual(self.model.stats()["to_delete"], before - 1)
        self.model.toggle_delete(0, 1)
        self.assertEqual(self.model.stats()["to_delete"], before)

    def test_set_keep_recomputes_decisions(self):
        self.model.set_keep(0, 2)
        row = self.model.row_at(0)
        self.assertEqual(row.keep, 2)
        self.assertEqual(row.items[2].decision, KEEP)
        self.assertEqual(row.items[0].decision, DELETE)

    def test_stats_are_consistent(self):
        s = self.model.stats()
        self.assertEqual(s["groups"], 2)
        self.assertEqual(s["files"], 5)
        self.assertEqual(s["to_delete"], 3)
        self.assertGreater(s["reclaim"], 0)

    def test_planned_excludes_oversized(self):
        m = GroupModel()
        m.set_rows(build_rows(self.entries, [[0, 1, 2]], "cluster", oversized=True))
        self.assertEqual(m.planned(), [], "超大聚类绝不能进入待删清单")
        self.assertEqual(m.stats()["to_delete"], 2, "但仍要有待删计数供界面显示")

    def test_planned_matches_decisions(self):
        planned = self.model.planned()
        self.assertEqual(len(planned), 3)
        self.assertTrue(all(isinstance(p, tuple) and len(p) == 2 for p in planned))

    def test_refilter(self):
        self.model.refilter({"perceptual"})
        self.assertEqual(self.model.visible_count(), 0)
        self.model.refilter(None)
        self.assertEqual(self.model.visible_count(), 2)

    def test_undo_last_clears_decisions(self):
        self.assertTrue(self.model.undo_last())
        self.assertEqual(self.model.stats()["to_delete"], 0)
        self.assertEqual(self.model.stats()["pending_groups"], 2)

    def test_apply_auto_restores_recommendation(self):
        self.model.undo_last()
        self.model.apply_auto_to_all()
        self.assertEqual(self.model.stats()["to_delete"], 3)


class TestQtAdapter(unittest.TestCase):
    """Qt 适配层很薄，但它是虚拟化的入口，值得钉住。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_rowcount_and_data(self):
        from PySide6.QtCore import Qt

        from imgdedup_ui.model import make_list_model

        entries = [ent(f"x{i}.png", size=1000 - i) for i in range(3)]
        store = GroupModel()
        store.set_rows(build_rows(entries, [[0, 1, 2]], "exact"))
        lm = make_list_model(store)
        self.assertEqual(lm.rowCount(), 1)
        row = lm.data(lm.index(0, 0), Qt.UserRole)
        self.assertIsInstance(row, GroupRow)

    def test_rowcount_follows_filter(self):
        from imgdedup_ui.model import make_list_model

        entries = [ent(f"y{i}.png", size=1000 - i) for i in range(3)]
        store = GroupModel()
        store.set_rows(build_rows(entries, [[0, 1, 2]], "exact"))
        lm = make_list_model(store)
        store.refilter({"semantic"})
        self.assertEqual(lm.rowCount(), 0)


class TestStageProgress(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_overall_is_weighted_by_totals(self):
        from imgdedup_ui.widgets import StageProgress

        p = StageProgress()
        p.set_stages([("A", 5, 10), ("B", 10, 10)])
        self.assertAlmostEqual(p.overall(), 15 / 20)

    def test_update_stage_replaces_not_appends(self):
        from imgdedup_ui.widgets import StageProgress

        p = StageProgress()
        p.set_stages([("A", 0, 10)])
        p.update_stage("A", 4, 10)
        p.update_stage("B", 1, 2)
        self.assertEqual(len(p.stages), 2)
        self.assertEqual(p.stages[0], ("A", 4, 10))

    def test_indeterminate_stage_does_not_break_overall(self):
        """遍历阶段只能报「已发现数」，总数未定（0），不能因此算出负进度或除零。"""
        from imgdedup_ui.widgets import StageProgress

        p = StageProgress()
        p.set_stages([("遍历", 300, 0)])
        self.assertGreaterEqual(p.overall(), 0.0)
        p.set_stages([("遍历", 300, 0), ("元数据", 5, 10)])
        self.assertAlmostEqual(p.overall(), 5 / 11)


class TestFilterSemantics(unittest.TestCase):
    """筛选项的哨兵值 + 默认状态。

    这里钉住的是一个真实 bug：「全部」项的 `Qt.UserRole` 曾经是 `None`，
    而收集勾选时用 `if k and ...` 过滤掉了它 → 只勾「全部」时收到空集
    → `refilter(set())` → 0 行 → 分组整片消失。
    `None` 必须留给「一张都不显示」，「全部」必须有自己的哨兵值。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _win(self):
        from imgdedup_ui.window import ALL_KINDS, MainWindow

        w = MainWindow()
        w.model.set_rows(build_rows(
            [ent(f"a{i}.png", size=1000 - i) for i in range(3)]
            + [ent(f"b{i}.png", size=500 - i, w=200, h=200) for i in range(2)],
            [[0, 1, 2], [3, 4]], "exact"))
        return w, ALL_KINDS

    def test_all_sentinel_is_not_none(self):
        w, ALL_KINDS = self._win()
        self.assertIsNotNone(ALL_KINDS, "「全部」的哨兵值不能是 None")
        self.assertEqual(w.filter_list.item(0).data(Qt.UserRole), ALL_KINDS)

    def test_initial_state_shows_everything(self):
        """刚扫完 → 「全部」默认选中，分组照常出现（用户报的那个 bug 的反面）。"""
        w, _ = self._win()
        self.assertEqual(w.filter_list.count(), 2, "全部 + 精确重复")
        self.assertIs(w.filter_list.currentItem(), w.filter_list.item(0),
                      "刚建好时应默认选中「全部」")
        self.assertEqual(w.model.visible_count(), 2, "「全部」选中时分组不能消失")
        self.assertIsNone(w._active_kinds(), "「全部」→ 不过滤（None）")

    def test_manual_all_returns_none(self):
        """显式选「全部」→ 不过滤。"""
        w, ALL_KINDS = self._win()
        w._select_only("exact")
        self.assertEqual(w.model.visible_count(), 2)
        w._select_only(ALL_KINDS)
        self.assertIsNone(w._active_kinds())
        self.assertEqual(w.model.visible_count(), 2)

    def test_kind_only_filters(self):
        """选中某个分类 → 走「按类型过滤」。"""
        w, _ = self._win()
        w._select_only("exact")
        self.assertEqual(w._active_kinds(), {"exact"})
        self.assertEqual(w.model.visible_count(), 2)

    def test_unknown_kind_falls_back_to_all(self):
        """选中一个不存在的分类 → 回落「全部」，不能变成"全过滤"（一张不显示）。"""
        w, _ = self._win()
        w._select_only("nonexistent")
        # `_select_only` 找不到就不动当前项 → 仍是「全部」
        self.assertIsNone(w._active_kinds())
        self.assertEqual(w.model.visible_count(), 2)

    def test_rebuild_preserves_user_selection(self):
        """裁决会触发 `_rebuild_filters()`：它不能把用户的选择打回「全部」。"""
        w, _ = self._win()
        w._select_only("exact")
        self.assertEqual(w._current_filter_key(), "exact")

        w.model.toggle_delete(0, 1)      # 任意一次 emit counts_changed 的操作
        self.assertEqual(w._current_filter_key(), "exact",
                         "重建筛选项后用户的选择被偷偷改回「全部」了")
        self.assertEqual(w._active_kinds(), {"exact"})

    def test_rebuild_when_selected_kind_disappears(self):
        """选中的分类在新结果里不存在了 → 回落「全部」，而不是卡在空白。

        真实场景：关掉语义层重扫后 `semantic` 组消失，而用户上次正选着它。
        """
        from imgdedup_ui.window import ALL_KINDS

        w, _ = self._win()
        w._select_only("exact")
        w.model.set_rows([])             # 重扫后一个组都没有
        self.assertEqual(w._current_filter_key(), ALL_KINDS)

    def test_single_selection_only(self):
        """单选语义：选中项永远只有 1 个，且不允许"一个都不选"。"""
        w, _ = self._win()
        w._select_only("exact")
        n = sum(1 for i in range(w.filter_list.count())
                if w.filter_list.item(i).isSelected())
        self.assertEqual(n, 1, "单选列表里选中项必须恰好 1 个")
        self.assertEqual(w.filter_list.selectionMode(),
                         QAbstractItemView.SingleSelection)



class TestFilterSingleSelection(unittest.TestCase):
    """筛选改为单选之后的交互契约。

    改造背景：原先「全部」与各分类是「全部 ⟺ 所有分类都勾选」的联动复选框，
    用户要求退化成单选框：「全部」或任意一个分类，二选一。

    这一组测试刻意走真实触发路径（`setCurrentItem` / 模拟点击），
    不手工摆 `checkState`：见 `TestFilterSemantics` 里那条通用规则：
    给状态加了不变量之后，测试就不能再手工摆状态。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _two_kinds():
        """造出两个分类：1 个精确组 + 1 个感知组（各自 2 张）。"""
        rows = build_rows([ent("e1.png", size=1000), ent("e2.png", size=900)],
                          [[0, 1]], "exact")
        rows += build_rows([ent("p1.png", size=500, w=200, h=200),
                            ent("p2.png", size=400, w=200, h=200)],
                           [[0, 1]], "perceptual")
        return rows

    def _win(self, two_kinds=False):
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        if two_kinds:
            w.model.set_rows(self._two_kinds())
        else:
            w.model.set_rows(build_rows(
                [ent(f"a{i}.png", size=1000 - i) for i in range(2)],
                [[0, 1]], "exact"))
        return w

    def test_clicking_kind_switches_selection(self):
        """点分类 → 选中项从「全部」变成该分类（而不是同时选中两个）。"""
        w = self._win(two_kinds=True)
        self.assertEqual(w.filter_list.count(), 3, "全部 + 2 个分类")

        w._select_only("perceptual")
        self.assertEqual(w.filter_list.currentItem().data(Qt.UserRole),
                         "perceptual")
        selected = [w.filter_list.item(i).isSelected()
                    for i in range(w.filter_list.count())]
        self.assertEqual(selected, [False, False, True])
        self.assertEqual(w._active_kinds(), {"perceptual"})

    def test_switching_back_to_all_restores(self):
        w = self._win(two_kinds=True)
        w._select_only("perceptual")
        w._select_only("__all__")
        self.assertIsNone(w._active_kinds())
        self.assertEqual(w.model.visible_count(), len(w.model.rows))

    def test_selection_change_signal_refilters(self):
        """信号真的接上了：直接 `setCurrentItem` 也要触发重筛。"""
        w = self._win(two_kinds=True)
        self.assertIsNone(w._active_kinds())

        w.filter_list.setCurrentItem(w.filter_list.item(2))   # 第二个分类
        self.assertEqual(w._active_kinds(), {"perceptual"})
        self.assertLess(w.model.visible_count(), len(w.model.rows))

    def test_no_checkboxes_left(self):
        """单选列表里不该再有复选框：有复选框会让人以为能多选。"""
        w = self._win()
        for i in range(w.filter_list.count()):
            it = w.filter_list.item(i)
            # `Qt.CheckStateRole` 未设置时 `data()` 返回 None
            self.assertIsNone(it.data(Qt.CheckStateRole),
                              f"第 {i} 项还带着复选框状态")

    def test_state_label_text(self):
        """状态文字随选择变化。

        `set_rows()` 只重建筛选项、不刷新这个标签（它由 `_refresh_*`
        系列负责）。所以断标签之前必须先显式触发一次刷新。
        """
        w = self._win()
        w._refresh_filter_state()
        self.assertIn("显示全部", w.lbl_filter_state.text())
        w._select_only("exact")
        self.assertIn("显示", w.lbl_filter_state.text())


class TestThumbnailSelection(unittest.TestCase):
    """缩略图级选中 + 命中测试。

    用户报的第三个 bug 是「左键点图片没有高亮，看不出选中了没有」。
    高亮本身只能靠截图看，但它依赖的状态机可以在这里钉死 ——
    尤其是「点到的到底是哪一张」。

    这一组曾漏掉一个静默故障：`hit_test()` 用 `self.parent()` 反查视图，
    而 delegate 的 parent 其实是 MainWindow 不是 QListView，
    于是 `hasattr(view, "indexAt")` 守卫失败 → 永远返回 None。
    缩略图看着能点，一次都没命中，还不报任何错。
    所以下面直接对真实 QListView做命中，而不是用替身。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _real(self, n: int = 6):
        """建一个真的 QListView + delegate，走真实坐标换算。"""
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QListView

        from imgdedup_ui.model import ThumbCache, make_list_model
        from imgdedup_ui.widgets import GroupDelegate

        entries = [ent(f"t{i}.png", size=1000 - i) for i in range(n)]
        store = GroupModel()
        store.set_rows(build_rows(entries, [list(range(n))], "exact"))
        store.refilter(None)
        view = QListView()
        view.setModel(make_list_model(store))
        thumbs = ThumbCache()
        d = GroupDelegate(store, thumbs, view)
        view.setItemDelegate(d)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        # 必须登记，交给 tearDown 收掉线程池。
        # 走 `show()` 会让 delegate 真的 paint 一次 → `thumbs.request()` 起后台任务；
        # 进程退出时这些任务还在跑，回来 emit 时 Qt 已经开始拆对象，
        # 表现为测试全绿但退出码 139（段错误）——
        # 只看 "OK" 会以为没事，CI 里却是红的。
        self.addCleanup(thumbs.shutdown)
        self._keep = (view, d, thumbs, store)
        return view, d, store

    def test_hit_test_uses_the_real_view(self):
        """钉住那个静默故障：delegate 必须真的能命中，而不是永远 None。"""
        from PySide6.QtCore import QRect

        view, d, store = self._real(6)
        self.assertIs(d.view, view, "delegate 没有持有视图引用")
        vr = view.visualRect(view.model().index(0, 0))
        self.assertTrue(vr.isValid() and vr.height() > 0, "视图还没布局")
        card = QRect(0, 0, vr.width(), vr.height())
        row = store.row_at(0)
        hits = []
        for pos in range(row.count):
            pt = d._item_rect(card, row, pos).center() + vr.topLeft()
            hits.append(d.hit_test(pt))
        self.assertEqual(hits, [(0, p) for p in range(row.count)],
                         f"命中测试错位或失效：{hits}")

    def test_card_header_is_not_a_thumbnail(self):
        from PySide6.QtCore import QPoint

        view, d, _store = self._real(6)
        vr = view.visualRect(view.model().index(0, 0))
        self.assertIsNone(d.hit_test(QPoint(vr.left() + 30, vr.top() + 12)),
                          "卡片头部不该被判成缩略图")

    def test_selection_state_and_caption(self):
        view, d, _store = self._real(6)
        self.assertFalse(d.is_selected(0, 3))
        d.set_selected(0, 3)
        self.assertTrue(d.is_selected(0, 3))
        self.assertFalse(d.is_selected(0, 2))
        self.assertFalse(d.is_selected(1, 3))
        d.clear_selected()
        self.assertFalse(d.is_selected(0, 3))

    def test_set_selected_updates_only_one_thumbnail(self):
        """选中态是"单张"，不是"整组"：同一时间只能有一张高亮。"""
        view, d, _store = self._real(6)
        d.set_selected(0, 2)
        d.set_selected(0, 5)
        self.assertTrue(d.is_selected(0, 5))
        self.assertFalse(d.is_selected(0, 2),
                         "换选中项后旧的那张还亮着")

    def test_set_undecided_only_for_non_keeper(self):
        store = GroupModel()
        store.set_rows(build_rows([ent(f"u{i}.png", size=1000 - i) for i in range(3)],
                                  [[0, 1, 2]], "exact"))
        row = store.row_at(0)
        store.set_undecided(0, row.keep)
        self.assertEqual(row.items[row.keep].decision, KEEP,
                         "保留项不能被退回未裁决")
        store.set_undecided(0, 1)
        self.assertEqual(row.items[1].decision, UNDECIDED)


class TestMultiKeep(unittest.TestCase):
    """一组可保留多张（用户要求「数量不限」）。

    这里钉住的是一条数据安全不变量：多保留之后，
    被保留的每一张都不能出现在待删清单里。以前 `keep` 是单个 int，
    `planned()` 只排除那一个下标；改成集合后漏改任何一处，
    都会变成「用户说保留的图被删掉」—— 静默且不可逆。
    """

    def _store(self, n: int = 5):
        store = GroupModel()
        store.set_rows(build_rows([ent(f"k{i}.png", size=1000 - i)
                                   for i in range(n)], [list(range(n))], "exact"))
        return store

    def test_keep_is_a_set(self):
        store = self._store()
        row = store.row_at(0)
        self.assertEqual(row.keeps, {row.keep}, "默认只保留推荐的那一张")

    def test_set_keeps_replaces(self):
        store = self._store()
        store.set_keeps(0, {1, 3})
        row = store.row_at(0)
        self.assertEqual(row.keeps, {1, 3})
        self.assertEqual(row.items[1].decision, KEEP)
        self.assertEqual(row.items[3].decision, KEEP)
        self.assertEqual(row.items[0].decision, DELETE,
                         "被换下来的旧保留项应该标删")

    def test_add_keep_appends(self):
        """多选「设为保留」是追加语义：不能把先选的那张顶掉。"""
        store = self._store()
        store.set_keeps(0, {2})
        store.add_keep(0, {3, 4})
        self.assertEqual(store.row_at(0).keeps, {2, 3, 4})

    def test_empty_keeps_is_rejected(self):
        """不变量 1：不能把保留集合清空。"""
        store = self._store()
        store.set_keeps(0, set())
        self.assertTrue(store.row_at(0).keeps, "保留集合被清空了")
        store.add_keep(0, set())
        self.assertTrue(store.row_at(0).keeps)
        store.toggle_keeps(0, set())
        self.assertTrue(store.row_at(0).keeps)

    def test_toggle_keeps_does_not_empty_the_group(self):
        """取消掉所有保留项时，必须自动回退到推荐的那一张。"""
        store = self._store()
        row = store.row_at(0)
        rec = row.keep
        store.toggle_keeps(0, set(row.keeps))
        self.assertTrue(row.keeps, "取消后保留集合空了")
        self.assertEqual(row.keeps, {rec})

    def test_planned_excludes_every_kept_item(self):
        """核心不变量：保留的每一张都不能进待删清单。"""
        store = self._store()
        store.set_keeps(0, {0, 2, 4})
        planned = {p for p, _ in store.planned()}
        row = store.row_at(0)
        for pos in (0, 2, 4):
            self.assertNotIn(row.items[pos].path, planned,
                             f"第 {pos} 张被标为保留，却进了待删清单")
        self.assertEqual(len(planned), 2)

    def test_toggle_delete_many_skips_keeps(self):
        """多选标删时保留项必须被跳过，而不是整批拒绝。

        先把非保留项退回未裁决：默认裁决下它们本来就是待删，
        直接调 `toggle_delete_many` 会走「整批取消删除」那条分支，
        测的就不是「标删时跳过保留项」了。
        """
        store = self._store()
        store.set_keeps(0, {0})
        row = store.row_at(0)
        row.items[1].decision = UNDECIDED
        row.items[2].decision = UNDECIDED
        store.toggle_delete_many(0, {0, 1, 2})
        self.assertEqual(row.items[0].decision, KEEP, "保留项被标删了")
        self.assertEqual(row.items[1].decision, DELETE)
        self.assertEqual(row.items[2].decision, DELETE)

    def test_toggle_delete_many_uniform_direction(self):
        """整批方向一致：混合状态下应统一成"取消删除"，而不是各自翻转。"""
        store = self._store()
        row = store.row_at(0)
        row.items[1].decision = DELETE
        row.items[2].decision = KEEP
        store.toggle_delete_many(0, {1, 2})
        self.assertEqual({row.items[1].decision, row.items[2].decision}, {KEEP},
                         "混合状态应整体取消删除标记")

    def test_reclaim_counts_multi_keep(self):
        store = self._store()
        row = store.row_at(0)
        store.set_keeps(0, {0, 1})
        expect = sum(i.size for p, i in enumerate(row.items) if p not in (0, 1))
        self.assertEqual(row.reclaim, expect)

    def test_keep_compat_property(self):
        """兼容属性 `keep` 返回最小的那个保留下标（给"推荐保留"提示用）。"""
        store = self._store()
        store.set_keeps(0, {3, 1})
        self.assertEqual(store.row_at(0).keep, 1)

    def test_is_kept(self):
        store = self._store()
        store.set_keeps(0, {2})
        row = store.row_at(0)
        self.assertTrue(row.is_kept(2))
        self.assertFalse(row.is_kept(1))

    def test_duplicate_position_in_set_is_harmless(self):
        """重复位置只是集合里的一个元素：不该产生重复删项。"""
        store = self._store()
        store.set_keeps(0, {1, 1})
        self.assertEqual(store.row_at(0).keeps, {1})
        self.assertEqual(len(store.planned()), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
