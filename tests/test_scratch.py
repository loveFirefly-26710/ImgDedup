"""`imgdedup_core/scratch.py`：删除路径 B（真删）。

这一层是护栏本身，所以测试重点全在「该拒绝的必须拒绝」。
所有真删都发生在 `.scratch/` 里（它本来就在 .gitignore 中）。
"""

import os
import shutil
import unittest

from imgdedup_core import scratch as SC


def _tree(root: str, files: int = 3, size: int = 128) -> str:
    os.makedirs(root, exist_ok=True)
    for i in range(files):
        with open(os.path.join(root, f"f{i}.bin"), "wb") as fh:
            fh.write(b"x" * size)
    sub = os.path.join(root, "sub")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "deep.bin"), "wb") as fh:
        fh.write(b"y" * size)
    return root


class ScratchCase(unittest.TestCase):

    def setUp(self):
        self.name = f"test_{self.__class__.__name__}_{id(self)}"
        self.path = SC.scratch_path(self.name)

    def tearDown(self):
        shutil.rmtree(self.path, ignore_errors=True)


class TestPaths(unittest.TestCase):

    def test_scratch_root_is_created(self):
        root = SC.scratch_root()
        self.assertTrue(root.is_dir())
        self.assertEqual(root.name, SC.SCRATCH_DIRNAME)

    def test_scratch_path_creates_parents(self):
        p = SC.scratch_path("a", "b", "c.bin")
        try:
            self.assertTrue(p.parent.is_dir())
            self.assertEqual(p.name, "c.bin")
        finally:
            shutil.rmtree(SC.scratch_root() / "a", ignore_errors=True)

    def test_allowed_roots_is_only_scratch(self):
        self.assertEqual(SC.ALLOWED_ROOTS, (".scratch",))


class TestAssertPurgeable(ScratchCase):

    def test_accepts_a_path_inside_scratch(self):
        _tree(self.path)
        self.assertEqual(SC._assert_purgeable(self.path), self.path)

    def test_refuses_missing_target(self):
        with self.assertRaises(SC.PurgeRefused) as cm:
            SC._assert_purgeable(os.path.join(self.path, "nope"))
        self.assertIn("不存在", str(cm.exception))

    def test_refuses_the_whitelist_root_itself(self):
        with self.assertRaises(SC.PurgeRefused) as cm:
            SC._assert_purgeable(SC.scratch_root())
        self.assertIn("白名单根目录", str(cm.exception))

    def test_refuses_anything_outside_the_whitelist(self):
        outside = SC.PROJECT_ROOT / "imgdedup_core"
        with self.assertRaises(SC.PurgeRefused) as cm:
            SC._assert_purgeable(outside)
        self.assertIn("不在允许真删的白名单之下", str(cm.exception))

    def test_refuses_the_project_root(self):
        with self.assertRaises(SC.PurgeRefused):
            SC._assert_purgeable(SC.PROJECT_ROOT)

    def test_prefix_lookalike_directory_is_outside(self):
        """`.scratch2` 与 `.scratch` 前缀相同但不是子目录，必须拒绝。"""
        lookalike = SC.PROJECT_ROOT / (SC.SCRATCH_DIRNAME + "2")
        lookalike.mkdir(exist_ok=True)
        try:
            with self.assertRaises(SC.PurgeRefused):
                SC._assert_purgeable(lookalike)
        finally:
            lookalike.rmdir()


class TestReparsePoint(unittest.TestCase):

    def test_plain_directory_is_not_a_reparse_point(self):
        p = SC.scratch_path("plain_dir_probe")
        try:
            self.assertFalse(SC.is_reparse_point(p))
        finally:
            shutil.rmtree(p, ignore_errors=True)


class TestCollectTree(ScratchCase):

    def test_counts_files_dirs_and_bytes(self):
        _tree(self.path, files=3, size=128)
        files, dirs, links, size = SC._collect_tree(self.path)
        self.assertEqual(len(files), 4)          # f0..f2 + sub/deep.bin
        self.assertGreaterEqual(len(dirs), 2)    # self.path 与 sub
        self.assertEqual(links, [])
        self.assertEqual(size, 4 * 128)


class TestPurge(ScratchCase):

    def test_dry_run_reports_but_keeps_everything(self):
        _tree(self.path, files=2, size=64)
        r = SC.purge(self.path, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["files"], 3)
        self.assertEqual(r["bytes"], 3 * 64)
        self.assertEqual(r["freed"], 0)
        self.assertTrue(os.path.exists(self.path))

    def test_real_purge_removes_the_tree(self):
        _tree(self.path, files=2, size=64)
        r = SC.purge(self.path)
        self.assertFalse(r["dry_run"])
        self.assertEqual(r["files"], 3)
        self.assertFalse(r["exists_after"])
        self.assertFalse(os.path.exists(self.path))

    def test_purge_refuses_outside_the_whitelist(self):
        outside = SC.PROJECT_ROOT / "README.md"
        with self.assertRaises(SC.PurgeRefused):
            SC.purge(outside)
        self.assertTrue(outside.exists())

    def test_purge_single_file(self):
        os.makedirs(self.path, exist_ok=True)
        f = os.path.join(self.path, "one.bin")
        with open(f, "wb") as fh:
            fh.write(b"z" * 32)
        r = SC.purge(f)
        self.assertEqual(r["files"], 1)
        self.assertEqual(r["bytes"], 32)
        self.assertFalse(os.path.exists(f))


class TestPurgeScratch(ScratchCase):

    def test_only_touches_entries_under_scratch(self):
        _tree(self.path, files=1, size=16)
        results = SC.purge_scratch(dry_run=True)
        paths = [r["path"] for r in results]
        self.assertIn(str(self.path), paths)
        self.assertTrue(os.path.exists(self.path))
        for p in paths:
            self.assertTrue(p.startswith(str(SC.scratch_root())))


class TestHuman(unittest.TestCase):

    def test_matches_the_core_formatter(self):
        from imgdedup_core import infra
        for n in (0, 1, 1023, 1024, 1536, 1024 ** 2, 1024 ** 5):
            with self.subTest(n=n):
                self.assertEqual(SC.human(n), infra.human_bytes(n))


class TestReport(unittest.TestCase):

    def test_report_prints_a_summary_line(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            SC.report([{"path": "x", "files": 2, "bytes": 2048},
                       {"path": "y", "refused": "不删"}], title="t")
        out = buf.getvalue()
        self.assertIn("[t]", out)
        self.assertIn("2.0 KB", out)
        self.assertIn("拒绝", out)


if __name__ == "__main__":
    unittest.main()
