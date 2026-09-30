"""护栏测试。

这组测试钉住两件不允许回退的事：
  1. 核心引擎不引入任何 GUI 依赖（保证可独立测试、可被替换）；
  2. 真删通道 purge() 不可能删到 .scratch 之外——包括通过目录联接逃逸。

运行： python tests\\test_guardrails.py -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgdedup_core import scratch as SC  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestCoreDecoupling(unittest.TestCase):
    """引擎必须与 GUI 无关。这是"能被独立测试"和"未来可替换实现"的前提。"""

    def test_no_gui_module_loaded_by_core(self):
        """必须在子进程里跑。

        原实现直接在测试进程里 `import imgdedup_core` 然后查 `sys.modules`，
        这在单独跑本文件时是对的，但一旦跑全量套件就必然误报：
        `test_ui_model.py` 在 `setUpClass` 里建了 `QApplication`
        （Qt 的 Application 对象不允许销毁后重建，`sys.modules` 也不复位），
        于是 `PySide6` 留在模块表里 → 核心引擎被冤枉成"引入了 GUI 依赖"。
        真正的断言是「导入核心引擎这个动作会不会拉起 PySide6」，
        而它只在干净进程里成立，所以用一个新进程去问同一个问题。
        """
        code = (
            "import sys;"
            "sys.path.insert(0, r'%s');"
            "import imgdedup_core;"
            "from imgdedup_core import catalog, decode, feature, group, infra, scan;"
            "bad = [m for m in ('PySide6', 'PyQt6', 'PyQt5', 'tkinter')"
            " if m in sys.modules];"
            "print(','.join(bad))" % PROJECT_ROOT
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=180)
        self.assertEqual(r.returncode, 0,
                         f"子进程导入核心引擎失败:\n{r.stderr}")
        loaded = [m for m in r.stdout.strip().split(",") if m]
        self.assertEqual(loaded, [], f"核心引擎引入了 GUI 依赖: {loaded}")

    def test_scratch_module_has_no_action_import(self):
        """两条删除路径必须物理隔离：scratch 不得引用 action。"""
        with open(SC.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("from .action", src)
        self.assertNotIn("import action", src)


class TestPurgeGuard(unittest.TestCase):
    """purge() 的白名单护栏。任何一条失守都意味着可能删到真实数据。"""

    def test_refuses_project_file(self):
        with self.assertRaises(SC.PurgeRefused):
            SC.purge(SC.PROJECT_ROOT / "PLAN.md")

    def test_refuses_scratch_root_itself(self):
        with self.assertRaises(SC.PurgeRefused):
            SC.purge(SC.scratch_root())

    def test_refuses_real_library(self):
        """真实图库路径必须被拒绝——这条是本次项目的核心安全断言。"""
        with self.assertRaises(SC.PurgeRefused):
            SC.purge(r"E:\1\test")

    def test_refuses_nonexistent(self):
        with self.assertRaises(SC.PurgeRefused):
            SC.purge(SC.scratch_root() / "__definitely_not_here__")

    def test_refuses_parent_traversal(self):
        with self.assertRaises(SC.PurgeRefused):
            SC.purge(SC.scratch_root() / ".." / "imgdedup_core")

    def test_deletes_inside_scratch(self):
        box = SC.scratch_root() / "_guard_ok"
        sub = box / "deep"
        os.makedirs(sub, exist_ok=True)
        for i in range(3):
            with open(os.path.join(sub, f"junk{i}.bin"), "wb") as fh:
                fh.write(b"x" * 1024)
        res = SC.purge(box)
        self.assertEqual(res["files"], 3)
        self.assertFalse(res["exists_after"])
        self.assertFalse(box.exists())

    def test_refuses_reparse_point_as_target(self):
        outside = tempfile.mkdtemp(prefix="imgdedup_guard_")
        try:
            link = SC.scratch_root() / "_guard_link_target"
            if link.exists():
                os.rmdir(link)
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), outside],
                               capture_output=True, text=True, errors="replace")
            if r.returncode != 0:
                self.skipTest(f"无法创建目录联接: {r.stderr or r.stdout}")
            with self.assertRaises(SC.PurgeRefused):
                SC.purge(link)
            os.rmdir(link)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_does_not_escape_through_nested_junction(self):
        """最关键的一条：.scratch 内的联接不得导致越界删除。"""
        outside = tempfile.mkdtemp(prefix="imgdedup_guard_")
        try:
            sentinel = os.path.join(outside, "MUST_SURVIVE.txt")
            with open(sentinel, "w", encoding="utf-8") as fh:
                fh.write("这是 .scratch 之外的真实数据")

            box = SC.scratch_root() / "_guard_junction"
            os.makedirs(box, exist_ok=True)
            with open(os.path.join(box, "own_junk.txt"), "w") as fh:
                fh.write("junk")

            link = os.path.join(box, "escape")
            r = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                               capture_output=True, text=True, errors="replace")
            if r.returncode != 0:
                self.skipTest(f"无法创建目录联接: {r.stderr or r.stdout}")

            SC.purge(box)

            self.assertTrue(os.path.exists(sentinel),
                            "越界删除！目录联接的目标被删掉了")
            self.assertFalse(box.exists())
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class TestByteIdentical(unittest.TestCase):
    """删除前的最后一道闸：逐字节确认。"""

    def test_same_and_different(self):
        from imgdedup_core import feature as F

        d = SC.scratch_root() / "_biteq"
        os.makedirs(d, exist_ok=True)
        a, b, c = (d / "a.bin"), (d / "b.bin"), (d / "c.bin")
        payload = os.urandom(300_000)
        for p, data in ((a, payload), (b, payload), (c, payload[:-1] + b"\x00")):
            with open(p, "wb") as fh:
                fh.write(data)
        self.assertTrue(F.byte_identical(str(a), str(b)))
        self.assertFalse(F.byte_identical(str(a), str(c)))
        SC.purge(d)


class TestTrueDelete(unittest.TestCase):
    """purge 必须真删，不能只是把文件丢进回收站。

    本机对照实验结论（os.unlink / shutil.rmtree / powershell Remove-Item /
    SetFileInformationByHandle 的 POSIX 语义删除 全部会被重定向进回收站，
    只有 cmd /c del 不会）。这条测试把这个事实钉死——如果哪天实现被改回
    os.unlink，它会立刻失败。
    """

    @staticmethod
    def _recycle_bin_paths_since(t0: float) -> list[str]:
        import struct

        drive = os.path.splitdrive(str(SC.PROJECT_ROOT))[0]
        rb = os.path.join(drive + os.sep, "$Recycle.Bin")
        if not os.path.isdir(rb):
            return []
        out = []
        for sid in os.listdir(rb):
            d = os.path.join(rb, sid)
            if not os.path.isdir(d):
                continue
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if not name.startswith("$I"):
                    continue
                p = os.path.join(d, name)
                try:
                    if os.stat(p).st_mtime < t0:
                        continue
                    with open(p, "rb") as fh:
                        raw = fh.read(8192)
                    if len(raw) < 28:
                        continue
                    nlen = struct.unpack("<I", raw[24:28])[0]
                    if not (0 < nlen <= 4096):
                        continue
                    out.append(raw[28:28 + nlen * 2]
                               .decode("utf-16-le", "replace").replace(chr(0), ""))
                except Exception:  # noqa: BLE001
                    continue
        return out

    def test_purge_does_not_land_in_recycle_bin(self):
        import time

        box = SC.scratch_root() / "_true_delete_test"
        os.makedirs(box / "sub", exist_ok=True)
        target = box / "sub" / "junk.bin"
        with open(target, "wb") as fh:
            fh.write(b"junk" * 4096)

        t0 = time.time() - 2
        SC.purge(box)

        self.assertFalse(box.exists(), "purge 后目标仍存在")
        dumped = self._recycle_bin_paths_since(t0)
        norm = {os.path.normcase(str(target))}
        hit = [p for p in dumped if os.path.normcase(p) in norm]
        self.assertEqual(hit, [], f"purge 的文件进了回收站，不是真删: {hit}")

    def test_purge_removes_empty_tree_completely(self):
        box = SC.scratch_root() / "_tree_test"
        (box / "a" / "b" / "c").mkdir(parents=True, exist_ok=True)
        for rel in ("a/x.bin", "a/b/y.bin", "a/b/c/z.bin"):
            with open(box / rel, "wb") as fh:
                fh.write(b"z" * 512)
        res = SC.purge(box)
        self.assertEqual(res["files"], 3)
        self.assertFalse(box.exists())


class TestSemanticOverMergeGuard(unittest.TestCase):
    """语义层的过合并护栏：这是语义去重最危险的地方。

    实测教训：在 E:\\1\\test（同题材图库，3197 张）上用余弦阈值 0.92，
    合并出一个 1554 张的巨型组：半个图库会被标成"可删除"。
    根因是该库中位图的最近邻相似度高达 0.9867，绝对阈值不可跨库移植。

    护栏要求：超过规模上限的连通分量必须被隔离到 oversized，
    绝不能混进可删除组。这里用合成嵌入验证，不需要模型。
    """

    @staticmethod
    def _entries(n):
        from imgdedup_core.scan import FileEntry

        return [FileEntry(path=f"x{i}", size=100, mtime_ns=0, ext=".jpg",
                          embed_ok=True) for i in range(n)]

    def test_oversized_component_is_quarantined(self):
        import numpy as np

        from imgdedup_core import embed as EMB

        n_big, n_small = 60, 3
        v_big = np.tile(np.array([1.0, 0.0], np.float32), (n_big, 1))
        v_small = np.tile(np.array([0.0, 1.0], np.float32), (n_small, 1))
        V = np.vstack([v_big, v_small])
        entries = self._entries(n_big + n_small)

        groups, oversized = EMB.cosine_groups(entries, V, threshold=0.9, max_group=20)

        self.assertEqual(len(oversized), 1, "超大连通分量没有被隔离")
        self.assertEqual(len(oversized[0]), n_big)
        self.assertEqual([len(g) for g in groups], [n_small],
                         "正常组应当只有那一组 3 张")

    def test_under_cap_stays_in_groups(self):
        import numpy as np

        from imgdedup_core import embed as EMB

        V = np.tile(np.array([1.0, 0.0], np.float32), (5, 1))
        groups, oversized = EMB.cosine_groups(self._entries(5), V,
                                              threshold=0.9, max_group=20)
        self.assertEqual(oversized, [])
        self.assertEqual([len(g) for g in groups], [5])

    def test_dissimilar_vectors_do_not_merge(self):
        import numpy as np

        from imgdedup_core import embed as EMB

        V = np.array([[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]], np.float32)
        groups, oversized = EMB.cosine_groups(self._entries(3), V, threshold=0.9)
        self.assertEqual(groups, [])
        self.assertEqual(oversized, [])


class TestModelResolution(unittest.TestCase):
    """模型解析：目录找得到、优先级正确、文件齐全。

    模型现在是外置组件（和 cv2 一样不打进包），新克隆的仓库里
    本来就没有它，所以这几条不能硬要求"模型必须在位"，
    否则 CI 上必然假红（把"按设计没装"报成"坏了"）。
    在位时严格校验；不在位时跳过并说明怎么补。
    """

    def setUp(self):
        from imgdedup_core import embed as EMB

        if not EMB.is_ready():
            self.skipTest("模型未安装（外置可选组件）—— "
                          "先跑 python -m dev.fetch_model")

    def test_dir_is_found(self):
        from imgdedup_core import embed as EMB

        root = EMB.bundled_root()
        self.assertIsNotNone(root, "模型能读到却解析不出根目录")
        self.assertTrue((root / EMB.repo_slug()).is_dir(),
                        f"目录下没有 {EMB.repo_slug()}")

    def test_all_model_files_present(self):
        from imgdedup_core import embed as EMB

        root = EMB.bundled_root()
        self.assertIsNotNone(root)
        missing = [n for n in EMB.MODEL_FILES.values()
                   if not (root / EMB.repo_slug() / n).exists()]
        self.assertEqual(missing, [], f"模型缺文件: {missing}")

    def test_placed_copy_wins_over_empty_cache(self):
        """把缓存指到空目录，解析结果仍必须是"放在 extra/ 的那份"。

        这条守的是优先级：用户自己放好的模型必须比缓存优先，
        否则会出现"放了却没用上"这种看不见的错位。
        """
        import os

        from imgdedup_core import embed as EMB

        old = os.environ.get("IMGDEDUP_MODEL_DIR")
        os.environ["IMGDEDUP_MODEL_DIR"] = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_nonexistent_cache")
        try:
            d = EMB.model_dir()
            ready = EMB.is_ready()
        finally:
            if old is None:
                os.environ.pop("IMGDEDUP_MODEL_DIR", None)
            else:
                os.environ["IMGDEDUP_MODEL_DIR"] = old

        self.assertTrue(ready, "缓存为空时应能直接用已放置的那份模型")
        self.assertEqual(d, EMB.bundled_root() / EMB.repo_slug(),
                         f"没有优先用已放置的那份，实际解析到 {d}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
