"""`imgdedup_ui/model.py`：分组行、删除决策与统计。

删除决策只活在界面这一层，所以这里也是「每组至少留 1 张」这条不变量的
实际落点 —— 用例重点全在它上面。
"""

import unittest

from imgdedup_core.scan import FileEntry

from imgdedup_ui import model as M


def _entry(path: str, *, size=100, width=10, height=10, fmt="PNG") -> FileEntry:
    e = FileEntry(path=path, size=size, mtime_ns=0, ext=".png")
    e.width, e.height, e.fmt = width, height, fmt
    return e


def _entries(n: int):
    return [_entry(f"C:/imgs/{i}.png", size=100 + i,
                   width=10 + i, height=10 + i) for i in range(n)]


class TestItemRow(unittest.TestCase):

    def test_dims(self):
        self.assertEqual(M.ItemRow("a", 1, width=30, height=40).dims, "30×40")
        self.assertEqual(M.ItemRow("a", 1).dims, "尺寸未知")

    def test_defaults_to_undecided(self):
        self.assertEqual(M.ItemRow("a", 1).decision, M.UNDECIDED)


class TestGroupRow(unittest.TestCase):

    def _row(self, n=3) -> M.GroupRow:
        return M.GroupRow(kind="exact", label="x",
                          items=[M.ItemRow(f"p{i}", 10 * (i + 1)) for i in range(n)])

    def test_count_and_keep(self):
        r = self._row()
        self.assertEqual(r.count, 3)
        self.assertEqual(r.keep, 0)
        r.keeps = {2, 1}
        self.assertEqual(r.keep, 1)

    def test_auto_decide_keeps_at_least_one(self):
        r = self._row()
        r.auto_decide()
        self.assertEqual(r.keeps, {0})
        self.assertEqual([i.decision for i in r.items],
                         [M.KEEP, M.DELETE, M.DELETE])

    def test_auto_decide_respects_an_explicit_keep_set(self):
        r = self._row()
        r.keeps = {1}
        r.auto_decide()
        self.assertEqual([i.decision for i in r.items],
                         [M.DELETE, M.KEEP, M.DELETE])

    def test_reclaim_counts_only_deletable_items(self):
        r = self._row()
        r.auto_decide()
        self.assertEqual(r.reclaim, 50)          # 20 + 30，保留项是 10
        r.keeps = {0, 2}
        r.auto_decide()
        self.assertEqual(r.reclaim, 20)

    def test_pending_and_clear_decisions(self):
        r = self._row()
        r.clear_decisions()
        self.assertEqual(r.pending(), 3)
        r.auto_decide()
        self.assertEqual(r.pending(), 0)

    def test_oversized_rows_are_not_deletable(self):
        r = M.GroupRow(kind="cluster", label="x", oversized=True)
        self.assertFalse(r.deletable)
        self.assertTrue(M.GroupRow(kind="exact", label="x").deletable)


class TestBuildRows(unittest.TestCase):

    def test_marks_the_recommended_keeper(self):
        es = _entries(3)
        rows = M.build_rows(es, [[0, 1, 2]], "exact")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.count, 3)
        self.assertEqual(row.keeps, {2})          # 2 号分辨率最高
        self.assertEqual(row.items[2].decision, M.KEEP)

    def test_skips_groups_smaller_than_two(self):
        self.assertEqual(M.build_rows(_entries(3), [[0], []], "exact"), [])

    def test_label_of_receives_entries_group_and_keeper(self):
        seen = {}

        def label_of(entries, group, keep):
            seen.update(n=len(entries), group=list(group), keep=keep)
            return "自定义"

        rows = M.build_rows(_entries(2), [[0, 1]], "exact", label_of=label_of)
        self.assertEqual(rows[0].label, "自定义")
        self.assertEqual(seen, {"n": 2, "group": [0, 1], "keep": 1})

    def test_default_label_is_the_kind(self):
        self.assertEqual(M.build_rows(_entries(2), [[0, 1]], "perceptual")[0].label,
                         "perceptual")

    def test_oversized_flag_is_carried_over(self):
        rows = M.build_rows(_entries(2), [[0, 1]], "cluster", oversized=True)
        self.assertTrue(rows[0].oversized)


class TestMergedLabel(unittest.TestCase):

    def test_reports_byte_duplicate_units(self):
        self.assertEqual(M.merged_label("exact", 2, 0), "含 2 组字节重复")
        self.assertEqual(M.merged_label("exact", 1, 1), "含 1 组字节重复")

    def test_falls_back_to_the_kind(self):
        self.assertEqual(M.merged_label("perceptual", 0, 2), "perceptual")
        self.assertEqual(M.merged_label("semantic", 1, 0), "semantic")


class TestBuildMergedRows(unittest.TestCase):

    def test_groups_are_sorted_by_kind_then_first_member(self):
        es = _entries(4)
        components = [("semantic", [2, 3]), ("exact", [0, 1])]
        rows = M.build_merged_rows(es, components, [[0, 1]])
        self.assertEqual([r.kind for r in rows], ["exact", "semantic"])

    def test_label_counts_byte_duplicate_units_and_free_members(self):
        es = _entries(4)
        rows = M.build_merged_rows(es, [("perceptual", [0, 1, 2, 3])], [[0, 1]])
        self.assertEqual(rows[0].label, "含 1 组字节重复")

    def test_members_without_a_byte_unit_are_reported_as_free(self):
        es = _entries(3)
        rows = M.build_merged_rows(es, [("perceptual", [0, 1, 2])], [])
        self.assertEqual(rows[0].label, "perceptual")

    def test_singletons_are_dropped(self):
        es = _entries(3)
        self.assertEqual(M.build_merged_rows(es, [("exact", [0])], []), [])

    def test_only_one_keeper_per_merged_row(self):
        es = _entries(4)
        rows = M.build_merged_rows(es, [("perceptual", [0, 1, 2, 3])], [[0, 1], [2, 3]])
        self.assertEqual(len(rows[0].keeps), 1)

    def test_no_member_belongs_to_two_rows(self):
        es = _entries(6)
        components = [("exact", [0, 1]), ("perceptual", [2, 3]), ("semantic", [4, 5])]
        rows = M.build_merged_rows(es, components, [[0, 1]])
        seen = [i.path for r in rows for i in r.items]
        self.assertEqual(len(seen), len(set(seen)))


class TestGroupModel(unittest.TestCase):

    def setUp(self):
        self.model = M.GroupModel()
        # 三行用互不相交的文件 —— 真实管线里过合并分量已经被隔离出去了，
        # 不会和普通分量共用成员。
        es = _entries(9)
        self.rows = (M.build_rows(es, [[0, 1, 2]], "exact")
                     + M.build_rows(es, [[3, 4, 5]], "perceptual")
                     + M.build_rows(es, [[6, 7, 8]], "cluster", oversized=True))
        self.model.set_rows(self.rows)

    def test_set_rows_makes_everything_visible(self):
        self.assertEqual(self.model.visible_count(), 3)
        self.assertEqual(len(self.model.visible_rows()), 3)
        self.assertIs(self.model.row_at(0), self.rows[0])

    def test_row_at_out_of_range_is_none(self):
        self.assertIsNone(self.model.row_at(99))
        self.assertIsNone(self.model.row_at(-1))

    def test_refilter_narrows_to_kinds(self):
        self.model.refilter({"exact"})
        self.assertEqual(self.model.visible_count(), 1)
        self.assertEqual(self.model.visible_rows()[0].kind, "exact")

    def test_refilter_none_shows_all_again(self):
        self.model.refilter({"exact"})
        self.model.refilter()
        self.assertEqual(self.model.visible_count(), 3)

    def test_set_keeps_keeps_at_least_one(self):
        row = self.model.row_at(0)
        self.model.set_keeps(0, {1})
        self.assertEqual(row.keeps, {1})
        # 空集合不合法：不变量是「每组至少留 1 张」
        self.model.set_keeps(0, set())
        self.assertEqual(row.keeps, {1})

    def test_set_keeps_rejects_out_of_range_positions(self):
        row = self.model.row_at(0)
        self.model.set_keeps(0, {99})
        self.assertEqual(row.keeps, {2})

    def test_toggle_keeps_can_add(self):
        row = self.model.row_at(0)
        self.model.toggle_keeps(0, {0})
        self.assertEqual(row.keeps, {0, 2})

    def test_toggle_keeps_never_empties_the_keep_set(self):
        row = self.model.row_at(0)
        self.model.set_keeps(0, {2})
        self.model.toggle_keeps(0, {2})
        self.assertEqual(len(row.keeps), 1)
        self.assertNotEqual(row.keeps, set())

    def test_toggle_delete_many_flips_decisions(self):
        row = self.model.row_at(0)
        # 初始是 [delete, delete, keep]；其中已经有 delete，于是整批翻成 keep
        self.model.toggle_delete_many(0, {0, 1})
        self.assertEqual([i.decision for i in row.items],
                         [M.KEEP, M.KEEP, M.KEEP])
        # 再翻一次，全都还不是 delete，于是整批设成 delete
        self.model.toggle_delete_many(0, {0, 1})
        self.assertEqual([i.decision for i in row.items],
                         [M.DELETE, M.DELETE, M.KEEP])

    def test_toggle_delete_many_cannot_touch_the_keeper(self):
        row = self.model.row_at(0)
        self.model.toggle_delete_many(0, {2})
        self.assertEqual(row.items[2].decision, M.KEEP)

    def test_set_undecided(self):
        row = self.model.row_at(0)
        self.model.set_undecided(0, 1)
        self.assertEqual(row.items[1].decision, M.UNDECIDED)

    def test_set_undecided_ignores_the_keeper(self):
        row = self.model.row_at(0)
        self.model.set_undecided(0, 2)
        self.assertEqual(row.items[2].decision, M.KEEP)

    def test_apply_auto_to_all_skips_oversized_rows(self):
        oversized = self.rows[2]
        oversized.clear_decisions()
        self.model.apply_auto_to_all()
        self.assertEqual(oversized.pending(), oversized.count)
        self.assertEqual(self.rows[0].pending(), 0)

    def test_undo_last_clears_decisions(self):
        self.assertTrue(self.model.undo_last())
        self.assertTrue(all(r.pending() == r.count for r in self.rows))
        self.assertFalse(self.model.undo_last())

    def test_stats_shape_and_values(self):
        st = self.model.stats()
        self.assertEqual(set(st), {"groups", "files", "to_delete",
                                   "reclaim", "pending_groups"})
        self.assertEqual(st["groups"], 3)
        self.assertEqual(st["files"], 9)
        self.assertEqual(st["pending_groups"], 0)

    def test_planned_excludes_oversized_and_kept(self):
        planned = self.model.planned()
        paths = [p for p, _s in planned]
        self.assertEqual(len(paths), len(set(paths)))
        oversized_paths = {i.path for i in self.rows[2].items}
        self.assertEqual(set(paths) & oversized_paths, set())
        for p, _s in planned:
            self.assertTrue(any(p == i.path for r in self.rows for i in r.items))

    def test_planned_is_empty_after_undo(self):
        self.model.undo_last()
        self.assertEqual(self.model.planned(), [])

    def test_signals_fire(self):
        hits = []
        self.model.changed.connect(lambda: hits.append("changed"))
        self.model.counts_changed.connect(lambda: hits.append("counts"))
        self.model.set_keeps(0, {1})
        self.assertIn("changed", hits)
        self.assertIn("counts", hits)


class TestKindVocabulariesAgree(unittest.TestCase):
    """四类 kind 的词表分散在四个模块里，键必须一致。

    键一旦漂移，筛选面板会列出报告里不存在的类别，而报告会漏掉界面能选的类别 ——
    两边都不报错。
    """

    def test_same_keys_everywhere(self):
        from imgdedup_ui import report, widgets, window

        keys = set(M.KIND_ORDER)
        self.assertEqual(keys, set(window.KIND_LABEL))
        self.assertEqual(keys, set(report.KIND_LABEL))
        self.assertEqual(keys, set(widgets.BADGE))

    def test_order_is_stable(self):
        self.assertEqual([k for k, _ in sorted(M.KIND_ORDER.items(),
                                               key=lambda kv: kv[1])],
                         ["exact", "perceptual", "semantic", "cluster"])


if __name__ == "__main__":
    unittest.main()
