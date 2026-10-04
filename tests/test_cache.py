"""`imgdedup_core/cache.py`：指纹复用库与缩略图缓存。

不碰真实图片：缩略图部分只验证路径约定与统计，不真的编解码 WebP。
"""

import os
import tempfile
import unittest

import numpy as np

from imgdedup_core import cache as K
from imgdedup_core.scan import FileEntry


def _entry(path: str, *, size: int = 10, mtime_ns: int = 100,
           width: int = 100, height: int = 50) -> FileEntry:
    e = FileEntry(path=path, size=size, mtime_ns=mtime_ns, ext=".png")
    e.width, e.height, e.bands = width, height, 3
    e.fmt = "PNG"
    return e


class TempCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imgdedup_cache_")
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()


class TestKeys(unittest.TestCase):

    def test_entry_key_is_stable_across_separator_and_case(self):
        a = K.entry_key("C:/Users/x/a.png", 10, 100)
        b = K.entry_key("C:\\Users\\x\\a.png", 10, 100)
        self.assertEqual(a, b)

    def test_entry_key_changes_with_size_or_mtime(self):
        base = K.entry_key("C:/a.png", 10, 100)
        self.assertNotEqual(base, K.entry_key("C:/a.png", 11, 100))
        self.assertNotEqual(base, K.entry_key("C:/a.png", 10, 101))

    def test_i64_u64_roundtrip(self):
        for v in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1):
            with self.subTest(v=v):
                self.assertEqual(K._u64(K._i64(v)), v)

    def test_i64_makes_value_fit_sqlite_signed_int(self):
        self.assertLess(K._i64((1 << 64) - 1), 0)


class TestFingerprintStore(TempCase):

    def _store(self):
        return K.FingerprintStore(os.path.join(self.root, "cache.db"))

    def test_roundtrip_fills_an_empty_entry(self):
        db = self._store()
        src = _entry("C:/img/a.png")
        src.phash, src.dhash = 7, 9
        src.hist = np.array([0.5, 0.25, 0.25], np.float32)
        src.feat_ok = True
        self.assertEqual(db.save([src]), 1)
        db.close()

        db2 = self._store()
        self.assertEqual(db2.load(), 1)
        fresh = _entry("C:/img/a.png", width=0, height=0)
        self.assertEqual(db2.apply([fresh]), 1)
        self.assertEqual((fresh.width, fresh.height, fresh.bands), (100, 50, 3))
        self.assertEqual((fresh.phash, fresh.dhash), (7, 9))
        self.assertTrue(fresh.feat_ok)
        self.assertTrue(fresh.feat_cached)
        db2.close()

    def test_mtime_change_invalidates_the_record(self):
        db = self._store()
        db.save([_entry("C:/img/a.png", mtime_ns=100)])
        db.load()
        moved = _entry("C:/img/a.png", mtime_ns=999)
        self.assertEqual(db.apply([moved]), 0)
        self.assertEqual(db.stats()["misses"], 1)
        db.close()

    def test_entries_without_anything_useful_are_not_saved(self):
        db = self._store()
        empty = _entry("C:/img/a.png", width=0, height=0)
        self.assertEqual(db.save([empty]), 0)
        db.close()

    def test_stats_shape(self):
        db = self._store()
        db.save([_entry("C:/img/a.png")])
        db.load()
        db.apply([_entry("C:/img/a.png"), _entry("C:/img/b.png")])
        st = db.stats()
        self.assertEqual(set(st), {"rows", "loaded", "hits", "misses"})
        self.assertEqual((st["rows"], st["loaded"]), (1, 1))
        self.assertEqual((st["hits"], st["misses"]), (1, 1))
        db.close()

    def test_load_replaces_previous_in_memory_table(self):
        db = self._store()
        db.save([_entry("C:/img/a.png")])
        self.assertEqual(db.load(), 1)
        db.save([_entry("C:/img/b.png")])
        self.assertEqual(db.load(), 2)
        db.close()

    def test_has_no_prune_method(self):
        # prune() 零引用，已移除；这条防它被加回来
        self.assertFalse(hasattr(K.FingerprintStore, "prune"))

    def test_len_matches_loaded_rows(self):
        db = self._store()
        db.save([_entry("C:/img/a.png"), _entry("C:/img/b.png")])
        self.assertEqual(len(db), 0)
        db.load()
        self.assertEqual(len(db), 2)
        db.close()


class TestThumbStore(TempCase):

    def test_key_depends_on_path_size_and_mtime(self):
        st = K.ThumbStore(self.root)
        base = st.key("C:/a.png", 256, 100)
        self.assertNotEqual(base, st.key("C:/b.png", 256, 100))
        self.assertNotEqual(base, st.key("C:/a.png", 128, 100))
        self.assertNotEqual(base, st.key("C:/a.png", 256, 101))
        self.assertTrue(base.endswith("-256"))

    def test_path_for_uses_a_two_char_shard(self):
        st = K.ThumbStore(self.root)
        key = st.key("C:/a.png", 256, 100)
        p = st.path_for(key)
        self.assertEqual(p.suffix, ".webp")
        self.assertEqual(p.stem, key)
        self.assertEqual(p.parent.name, key[:2])
        self.assertEqual(p.parent.parent, st.root)

    def test_stats_start_at_zero(self):
        self.assertEqual(K.ThumbStore(self.root).stats(),
                         {"hits": 0, "misses": 0, "writes": 0})

    def test_get_on_empty_cache_counts_a_miss(self):
        st = K.ThumbStore(self.root)
        self.assertIsNone(st.get("deadbeef-256"))
        self.assertEqual(st.stats()["misses"], 1)

    def test_has_no_clear_method(self):
        # 界面上没有清缓存入口，clear() 零引用，已移除
        self.assertFalse(hasattr(K.ThumbStore, "clear"))


class TestShouldSkipFeatures(unittest.TestCase):

    def test_threshold(self):
        self.assertTrue(K.should_skip_features(K.DEFAULT_MAX_FEATURE_PIXELS + 1))
        self.assertFalse(K.should_skip_features(K.DEFAULT_MAX_FEATURE_PIXELS))

    def test_limit_zero_disables_the_skip(self):
        self.assertFalse(K.should_skip_features(10 ** 18, limit=0))

    def test_default_limit_is_100_megapixels(self):
        self.assertEqual(K.DEFAULT_MAX_FEATURE_PIXELS, 100_000_000)


if __name__ == "__main__":
    unittest.main()
