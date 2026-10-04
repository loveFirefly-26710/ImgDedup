"""`imgdedup_core/group.py`：并查集、分层合并、精确/感知分组、推荐保留项。

不依赖真实图片：感知层的输入是直接构造的 `phash` / `dhash` / `hist`。
"""

import unittest

import numpy as np

from imgdedup_core import group as G
from imgdedup_core.scan import FileEntry


def _entry(path="a.png", size=100, *, phash=0, dhash=0, hist=None,
           feat_ok=True, fmt="PNG", mtime_ns=0, width=10, height=10):
    e = FileEntry(path=path, size=size, mtime_ns=mtime_ns, ext=".png")
    e.feat_ok = feat_ok
    e.phash = phash
    e.dhash = dhash
    e.hist = hist if hist is not None else np.array([1.0, 0.0, 0.0], np.float32)
    e.fmt = fmt
    e.width = width
    e.height = height
    return e


class TestUnionFind(unittest.TestCase):

    def test_starts_disjoint(self):
        uf = G.UnionFind(3)
        self.assertEqual(uf.groups(), [])
        self.assertNotEqual(uf.find(0), uf.find(1))

    def test_union_merges(self):
        uf = G.UnionFind(4)
        uf.union(0, 1)
        uf.union(2, 3)
        self.assertEqual(uf.groups(), [[0, 1], [2, 3]])

    def test_union_is_idempotent(self):
        uf = G.UnionFind(2)
        uf.union(0, 1)
        uf.union(1, 0)
        self.assertEqual(uf.groups(), [[0, 1]])

    def test_min_size_filters_singletons(self):
        uf = G.UnionFind(3)
        uf.union(0, 1)
        # min_size=1 把每个分量都吐出来，已经合并的仍是合并后的样子
        self.assertEqual(uf.groups(min_size=1), [[0, 1], [2]])
        self.assertEqual(uf.groups(min_size=2), [[0, 1]])

    def test_find_is_stable_after_merges(self):
        uf = G.UnionFind(5)
        uf.union(0, 1)
        uf.union(1, 2)
        root = uf.find(0)
        self.assertEqual(uf.find(1), root)
        self.assertEqual(uf.find(2), root)


class TestLayeredClusters(unittest.TestCase):

    def test_component_kind_is_the_weakest_layer(self):
        lc = G.LayeredClusters(3)
        lc.add_layer([[0, 1]], "exact")
        lc.add_layer([[1, 2]], "perceptual")
        self.assertEqual(lc.components(), [("perceptual", [0, 1, 2])])

    def test_redundant_edges_do_not_downgrade(self):
        # 感知层想把已经同属一个精确分量的两个点再连一次：
        # 没有产生新边，就不该把这一组降级成「近似重复」。
        lc = G.LayeredClusters(2)
        lc.add_layer([[0, 1]], "exact")
        added = lc.add_layer([[0, 1]], "perceptual")
        self.assertEqual(added, 0)
        self.assertEqual(lc.components(), [("exact", [0, 1])])

    def test_merges_counts_effective_edges_only(self):
        lc = G.LayeredClusters(4)
        lc.add_layer([[0, 1, 2]], "exact")      # 2 条边
        lc.add_layer([[0, 1, 2]], "perceptual")  # 全冗余，0 条
        lc.add_layer([[2, 3]], "semantic")       # 1 条
        self.assertEqual(lc.merges("exact"), 2)
        self.assertEqual(lc.merges("perceptual"), 0)
        self.assertEqual(lc.merges("semantic"), 1)

    def test_merges_unknown_kind_is_zero(self):
        self.assertEqual(G.LayeredClusters(2).merges("nope"), 0)

    def test_singletons_are_dropped(self):
        lc = G.LayeredClusters(3)
        lc.add_layer([[0, 1]], "exact")
        kinds = [k for k, _ in lc.components()]
        self.assertEqual(len(kinds), 1)

    def test_three_layers_do_not_overlap(self):
        lc = G.LayeredClusters(6)
        lc.add_layer([[0, 1]], "exact")
        lc.add_layer([[1, 2]], "perceptual")
        lc.add_layer([[3, 4]], "semantic")
        seen: list[int] = []
        for _kind, members in lc.components():
            seen += members
        self.assertEqual(len(seen), len(set(seen)), "同一文件出现在多个分量里")


class TestExactGroups(unittest.TestCase):

    def test_buckets_by_size_and_full_hash(self):
        a = _entry("a.png", 10); a.full = b"\x01"
        b = _entry("b.png", 10); b.full = b"\x01"
        c = _entry("c.png", 10); c.full = b"\x02"
        d = _entry("d.png", 11); d.full = b"\x01"
        self.assertEqual(G.exact_groups([a, b, c, d]), [[0, 1]])

    def test_entries_without_full_hash_are_skipped(self):
        a = _entry("a.png", 10)
        b = _entry("b.png", 10)
        self.assertEqual(G.exact_groups([a, b]), [])

    def test_hardlink_protected_indices_are_excluded(self):
        a = _entry("a.png", 10); a.full = b"\x01"
        b = _entry("b.png", 10); b.full = b"\x01"
        self.assertEqual(G.exact_groups([a, b], hardlink_protected={0}), [])
        self.assertEqual(G.exact_groups([a, b], hardlink_protected={1}), [])


class TestPerceptualParams(unittest.TestCase):

    def test_defaults(self):
        p = G.PerceptualParams()
        self.assertEqual((p.phash_max, p.dhash_max, p.hist_max, p.block),
                         (6, 12, 0.35, 256))


class TestPerceptualGroups(unittest.TestCase):

    def test_identical_features_group_together(self):
        es = [_entry(f"{i}.png", phash=12345, dhash=678) for i in range(3)]
        self.assertEqual(G.perceptual_groups(es), [[0, 1, 2]])

    def test_far_phash_does_not_group(self):
        es = [_entry("a.png", phash=0), _entry("b.png", phash=(1 << 64) - 1)]
        self.assertEqual(G.perceptual_groups(es), [])

    def test_far_histogram_blocks_grouping(self):
        # pHash / dHash 完全一致，但颜色分布天差地别 —— 第三道闸该拦住
        es = [_entry("a.png", hist=np.array([1.0, 0.0, 0.0], np.float32)),
              _entry("b.png", hist=np.array([0.0, 1.0, 0.0], np.float32))]
        self.assertEqual(G.perceptual_groups(es), [])
        off = G.PerceptualParams(hist_max=1.0)
        self.assertEqual(G.perceptual_groups(es, off), [[0, 1]])

    def test_feat_not_ok_is_skipped(self):
        es = [_entry("a.png"), _entry("b.png", feat_ok=False)]
        self.assertEqual(G.perceptual_groups(es), [])

    def test_fewer_than_two_candidates(self):
        self.assertEqual(G.perceptual_groups([_entry("a.png")]), [])

    def test_exclude_removes_candidates(self):
        es = [_entry(f"{i}.png") for i in range(3)]
        self.assertEqual(G.perceptual_groups(es, exclude={0}), [[1, 2]])

    def test_block_size_does_not_change_the_result(self):
        es = [_entry(f"{i}.png", phash=7) for i in range(5)]
        one_shot = G.perceptual_groups(es, G.PerceptualParams(block=256))
        chunked = G.perceptual_groups(es, G.PerceptualParams(block=2))
        self.assertEqual(one_shot, chunked)


class TestKeeper(unittest.TestCase):

    def test_prefers_more_pixels(self):
        es = [_entry("small.png", 100, width=10, height=10),
              _entry("big.png", 100, width=100, height=100)]
        self.assertEqual(G.recommend_keeper(es, [0, 1]), 1)

    def test_breaks_tie_by_size(self):
        es = [_entry("a.png", 100, width=10, height=10),
              _entry("b.png", 200, width=10, height=10)]
        self.assertEqual(G.recommend_keeper(es, [0, 1]), 1)

    def test_prefer_lossless_can_be_turned_off(self):
        # 像素、大小、时间全打平，只剩「无损格式」这一项能分高下
        es = [_entry("a.jpg", 100, fmt="JPEG", width=10, height=10),
              _entry("b.png", 100, fmt="PNG", width=10, height=10)]
        es[0].path = "a.jpg"
        self.assertEqual(G.recommend_keeper(es, [0, 1]), 1)
        # 关掉之后完全打平，max() 取先出现的那个
        self.assertEqual(G.recommend_keeper(es, [0, 1], prefer_lossless=False), 0)

    def test_reason_is_never_empty(self):
        es = [_entry("a.png", 100, width=10, height=10),
              _entry("b.png", 50, width=5, height=5)]
        reason = G.keeper_reason(es, [0, 1], 0)
        self.assertTrue(reason)
        self.assertIn("分辨率最高", reason)

    def test_reason_falls_back_when_nothing_stands_out(self):
        # 保留项既不是最大像素、也不是最大文件、还不是无损格式
        es = [_entry("a.png", 200, fmt="PNG", width=20, height=20),
              _entry("b.jpg", 50, fmt="JPEG", width=10, height=10)]
        es[1].path = "b.jpg"
        self.assertEqual(G.keeper_reason(es, [0, 1], 1), "综合评分最高")


if __name__ == "__main__":
    unittest.main()
