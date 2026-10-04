"""`imgdedup_core/embed.py`：CLIP 语义向量与余弦聚类。

不下载模型、不跑推理：`cosine_groups` 的输入是直接构造的单位向量。
"""

import os
import unittest

import numpy as np

from imgdedup_core import embed as E
from imgdedup_core.scan import FileEntry


def _entry(i: int, *, ok: bool = True) -> FileEntry:
    e = FileEntry(path=f"C:/imgs/{i}.png", size=10, mtime_ns=0, ext=".png")
    e.embed_ok = ok
    return e


def _unit(*vals) -> np.ndarray:
    v = np.array(vals, dtype=np.float32)
    return v / np.linalg.norm(v)


class TestPaths(unittest.TestCase):

    def test_repo_slug(self):
        self.assertEqual(E.repo_slug("Xenova/clip-vit-base-patch32"),
                         "Xenova__clip-vit-base-patch32")

    def test_model_root_honours_env_override(self):
        old = os.environ.get("IMGDEDUP_MODEL_DIR")
        os.environ["IMGDEDUP_MODEL_DIR"] = os.path.join("D:", "somewhere")
        try:
            self.assertEqual(str(E.model_root()), os.path.join("D:", "somewhere"))
        finally:
            if old is None:
                os.environ.pop("IMGDEDUP_MODEL_DIR", None)
            else:
                os.environ["IMGDEDUP_MODEL_DIR"] = old

    def test_model_extra_dir_sits_under_extra(self):
        self.assertEqual(E.model_extra_dir().name, "models")
        self.assertEqual(E.model_extra_dir().parent.name, "extra")

    def test_model_dir_always_ends_with_the_slug(self):
        d = E.model_dir()
        self.assertEqual(d.name, E.repo_slug())

    def test_is_bundled_matches_bundled_root(self):
        root = E.bundled_root()
        if root is None:
            self.assertFalse(E.is_bundled())
            return
        want = all((root / E.repo_slug() / n).exists()
                   for n in E.MODEL_FILES.values())
        self.assertEqual(E.is_bundled(), want)

    def test_model_files_lists_the_onnx_and_configs(self):
        names = set(E.MODEL_FILES.values())
        self.assertIn("vision_model.onnx", names)
        self.assertIn("config.json", names)


class TestConcatResults(unittest.TestCase):

    def test_empty_input(self):
        r = E.concat_results([])
        self.assertTrue(r.ok)
        self.assertEqual(r.vectors.shape, (0, 0))

    def test_concatenates_rows(self):
        a = E.EmbedResult(vectors=np.ones((2, 3), np.float32), dim=3, ok=True)
        b = E.EmbedResult(vectors=np.zeros((1, 3), np.float32), dim=3, ok=True)
        r = E.concat_results([a, b])
        self.assertEqual(r.vectors.shape, (3, 3))
        self.assertEqual(r.dim, 3)

    def test_failed_parts_are_dropped(self):
        good = E.EmbedResult(vectors=np.ones((2, 3), np.float32), dim=3, ok=True)
        bad = E.EmbedResult(vectors=None, dim=0, ok=False, note="炸了")
        self.assertEqual(E.concat_results([bad, good]).vectors.shape, (2, 3))

    def test_all_parts_failed(self):
        bad = E.EmbedResult(vectors=None, dim=0, ok=False)
        self.assertEqual(E.concat_results([bad]).vectors.shape, (0, 0))


class TestDefaults(unittest.TestCase):

    def test_threshold_and_group_cap(self):
        self.assertEqual(E.DEFAULT_THRESHOLD, 0.97)
        self.assertEqual(E.DEFAULT_MAX_GROUP, 200)


class TestCosineGroups(unittest.TestCase):

    def test_similar_entries_group_and_different_ones_do_not(self):
        entries = [_entry(0), _entry(1), _entry(2)]
        v = np.stack([_unit(1, 0), _unit(1, 0.01), _unit(0, 1)])
        groups, oversized = E.cosine_groups(entries, v)
        self.assertEqual(groups, [[0, 1]])
        self.assertEqual(oversized, [])

    def test_embed_ok_is_the_selection_mask(self):
        """只有 `embed_ok` 为真的项参与聚类，而且下标要和 vectors 对齐。

        这个字段曾经被当成「只写不读」误删过 —— 删掉之后
        `cosine_groups` 会拿到空列表，语义层静默失效。这条钉住它。
        """
        entries = [_entry(0, ok=False), _entry(1), _entry(2)]
        v = np.stack([_unit(1, 0), _unit(1, 0)])   # 只有 2 行，对应 idx=[1,2]
        groups, _ = E.cosine_groups(entries, v)
        self.assertEqual(groups, [[1, 2]])

    def test_no_usable_entries(self):
        groups, oversized = E.cosine_groups([_entry(0, ok=False)],
                                            np.zeros((0, 0), np.float32))
        self.assertEqual((groups, oversized), ([], []))

    def test_single_entry_cannot_form_a_group(self):
        groups, _ = E.cosine_groups([_entry(0)], np.stack([_unit(1, 0)]))
        self.assertEqual(groups, [])

    def test_oversized_group_is_quarantined(self):
        entries = [_entry(i) for i in range(4)]
        v = np.stack([_unit(1, 0)] * 4)
        groups, oversized = E.cosine_groups(entries, v, max_group=2)
        self.assertEqual(groups, [])
        self.assertEqual(oversized, [[0, 1, 2, 3]])

    def test_max_group_zero_disables_the_cap(self):
        entries = [_entry(i) for i in range(4)]
        v = np.stack([_unit(1, 0)] * 4)
        groups, oversized = E.cosine_groups(entries, v, max_group=0)
        self.assertEqual(groups, [[0, 1, 2, 3]])
        self.assertEqual(oversized, [])

    def test_threshold_is_respected(self):
        entries = [_entry(0), _entry(1)]
        v = np.stack([_unit(1, 0), _unit(1, 0.5)])   # 余弦约 0.894
        self.assertEqual(E.cosine_groups(entries, v, threshold=0.99)[0], [])
        self.assertEqual(E.cosine_groups(entries, v, threshold=0.5)[0], [[0, 1]])

    def test_exclude_removes_entries(self):
        entries = [_entry(i) for i in range(3)]
        v = np.stack([_unit(1, 0)] * 3)
        groups, _ = E.cosine_groups(entries, v, exclude={0})
        self.assertEqual(groups, [[1, 2]])

    def test_block_size_does_not_change_the_result(self):
        entries = [_entry(i) for i in range(5)]
        v = np.stack([_unit(1, 0)] * 5)
        self.assertEqual(E.cosine_groups(entries, v, block=512)[0],
                         E.cosine_groups(entries, v, block=2)[0])


if __name__ == "__main__":
    unittest.main()
