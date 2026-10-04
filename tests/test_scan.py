"""`imgdedup_core/scan.py`：目录遍历、噪声过滤、文件身份、硬链接与分桶。"""

import os
import tempfile
import unittest

from imgdedup_core import scan as S


def _write(path: str, size: int = 16) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    return path


class TestFileEntry(unittest.TestCase):

    def test_pixels_and_identity(self):
        e = S.FileEntry(path="a.png", size=1, mtime_ns=2, ext=".png",
                        dev=7, ino=9, width=30, height=40)
        self.assertEqual(e.pixels, 1200)
        self.assertEqual(e.identity, (7, 9))

    def test_defaults(self):
        e = S.FileEntry(path="a", size=1, mtime_ns=2, ext="")
        self.assertEqual(e.pixels, 0)
        self.assertIsNone(e.quick)
        self.assertIsNone(e.full)
        self.assertFalse(e.feat_ok)
        self.assertFalse(e.feat_cached)

    def test_has_no_write_only_embed_fields(self):
        # embed / embed_note / cached 是只写不读的字段，已移除。
        # 这条断言防的是它们被重新加回来。
        for name in ("embed", "embed_note", "cached"):
            with self.subTest(field=name):
                self.assertFalse(hasattr(S.FileEntry("a", 1, 2, ""), name))


class TestConstants(unittest.TestCase):

    def test_exclude_dirs(self):
        for name in (".git", "__pycache__", "node_modules", "$RECYCLE.BIN"):
            self.assertIn(name, S.DEFAULT_EXCLUDE_DIRS)

    def test_exclude_prefix(self):
        self.assertEqual(S.DEFAULT_EXCLUDE_PREFIX, ("~$", "._"))


class TestWalk(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imgdedup_scan_")
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_collects_files_and_lowercases_ext(self):
        _write(os.path.join(self.root, "A.JPG"), 10)
        _write(os.path.join(self.root, "sub", "b.png"), 20)
        got = {os.path.basename(e.path): e for e in S.walk(self.root)}
        self.assertEqual(set(got), {"A.JPG", "b.png"})
        self.assertEqual(got["A.JPG"].ext, ".jpg")
        self.assertEqual(got["b.png"].ext, ".png")
        self.assertEqual(got["b.png"].size, 20)

    def test_skips_noise_dirs_and_prefixes(self):
        _write(os.path.join(self.root, "keep.png"), 5)
        _write(os.path.join(self.root, "__pycache__", "c.png"), 5)
        _write(os.path.join(self.root, ".git", "d.png"), 5)
        _write(os.path.join(self.root, "node_modules", "e.png"), 5)
        _write(os.path.join(self.root, "~$lock.png"), 5)
        _write(os.path.join(self.root, "._macos.png"), 5)
        names = {os.path.basename(e.path) for e in S.walk(self.root)}
        self.assertEqual(names, {"keep.png"})

    def test_skips_zero_byte_files(self):
        _write(os.path.join(self.root, "empty.png"), 0)
        _write(os.path.join(self.root, "full.png"), 1)
        names = {os.path.basename(e.path) for e in S.walk(self.root)}
        self.assertEqual(names, {"full.png"})

    def test_min_size_is_respected(self):
        _write(os.path.join(self.root, "small.png"), 3)
        _write(os.path.join(self.root, "big.png"), 30)
        names = {os.path.basename(e.path) for e in S.walk(self.root, min_size=10)}
        self.assertEqual(names, {"big.png"})

    def test_custom_exclude_dirs_replaces_default(self):
        _write(os.path.join(self.root, "node_modules", "e.png"), 5)
        _write(os.path.join(self.root, "custom", "f.png"), 5)
        names = {os.path.basename(e.path) for e in S.walk(self.root,
                                                          exclude_dirs={"custom"})}
        self.assertEqual(names, {"e.png"})

    def test_records_device_and_inode(self):
        _write(os.path.join(self.root, "a.png"), 5)
        e = next(iter(S.walk(self.root)))
        self.assertNotEqual(e.dev, 0)
        self.assertNotEqual(e.ino, 0)


class TestHardlinks(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imgdedup_hl_")
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_hardlinked_pair_is_reported_as_one_group(self):
        a = _write(os.path.join(self.root, "a.png"), 8)
        b = os.path.join(self.root, "b.png")
        try:
            os.link(a, b)
        except OSError as e:
            self.skipTest(f"该卷/文件系统不支持硬链接：{e}")
        entries = list(S.walk(self.root))
        groups = S.find_hardlink_groups(entries)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(next(iter(groups.values()))), 2)

    def test_single_file_is_not_a_group(self):
        _write(os.path.join(self.root, "a.png"), 8)
        _write(os.path.join(self.root, "b.png"), 8)
        self.assertEqual(S.find_hardlink_groups(list(S.walk(self.root))), {})


class TestBucketBySize(unittest.TestCase):

    def test_buckets_by_size(self):
        es = [S.FileEntry(path=f"{i}.png", size=s, mtime_ns=0, ext=".png")
              for i, s in enumerate([10, 20, 10])]
        buckets = S.bucket_by_size(es)
        self.assertEqual(sorted(buckets), [10, 20])
        self.assertEqual(len(buckets[10]), 2)
        self.assertEqual(len(buckets[20]), 1)


if __name__ == "__main__":
    unittest.main()
