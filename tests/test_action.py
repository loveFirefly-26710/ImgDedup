"""`imgdedup_core/action.py`：删除路径 A（永不真删）。

覆盖：路径校验、计划去重、dry-run 的副作用边界、移动式删除的字节守恒、
撤销的幂等与冲突处理、回收站去向的端到端往返。

所有文件操作都在临时目录里，不碰真实图库。
"""

import json
import os
import sys
import tempfile
import unittest

from imgdedup_core import action as A

CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def _write(path: str, data: bytes = b"payload") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TempCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imgdedup_action_")
        self.root = self.tmp.name
        self.manifests = os.path.join(self.root, "_manifests")

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, rels, **kw):
        pairs = []
        for rel in rels:
            p = os.path.join(self.root, rel)
            pairs.append((p, os.path.getsize(p)))
        return A.plan_from_paths(pairs, root=self.root, **kw)


class TestUlid(unittest.TestCase):

    def test_shape_and_alphabet(self):
        u = A.new_ulid()
        self.assertEqual(len(u), 26)
        self.assertTrue(set(u) <= CROCKFORD, f"出现字母表之外的字符：{u}")

    def test_unique(self):
        self.assertEqual(len({A.new_ulid() for _ in range(500)}), 500)


class TestUnderRoot(unittest.TestCase):

    def test_self_and_children(self):
        root = os.path.join(self.__class__.__name__, "root")
        self.assertTrue(A._under_root(root, root))
        self.assertTrue(A._under_root(os.path.join(root, "a", "b.png"), root))

    def test_outside(self):
        self.assertFalse(A._under_root(os.path.dirname("C:/x"), "C:/x"))

    def test_sibling_with_shared_prefix_is_outside(self):
        # `C:\a` 与 `C:\ab` 前缀相同但不是父子关系 —— 靠拼 os.sep 区分
        self.assertFalse(A._under_root(os.path.join("C:/ab", "f.png"), "C:/a"))

    def test_is_case_insensitive_on_windows(self):
        if sys.platform != "win32":
            self.skipTest("仅 Windows 上 normcase 会折叠大小写")
        self.assertTrue(A._under_root("C:/Root/Sub/f.png", "c:/root"))


class TestDecision(unittest.TestCase):

    def test_save_then_load(self):
        store: dict = {}
        A.save_decision(lambda k, v: store.__setitem__(k, v),
                        A.Decision(mode=A.MODE_MOVE, target_dir="D:/out",
                                   remember=True))
        d = A.load_decision(lambda k, default: store.get(k, default))
        self.assertIsNotNone(d)
        self.assertEqual(d.mode, A.MODE_MOVE)
        self.assertEqual(d.target_dir, "D:/out")
        self.assertTrue(d.remember)

    def test_not_remembered_returns_none(self):
        store = {A.SETTING_REMEMBER: False}
        self.assertIsNone(A.load_decision(lambda k, d: store.get(k, d)))

    def test_unknown_mode_returns_none(self):
        store = {A.SETTING_REMEMBER: True, A.SETTING_MODE: "teleport"}
        self.assertIsNone(A.load_decision(lambda k, d: store.get(k, d)))

    def test_recycle_mode_drops_target_dir(self):
        store: dict = {}
        A.save_decision(lambda k, v: store.__setitem__(k, v),
                        A.Decision(mode=A.MODE_RECYCLE, target_dir="D:/ignored",
                                   remember=True))
        self.assertEqual(store[A.SETTING_TARGET], "")

    def test_default_target_dir_keeps_a_timestamp_level(self):
        d = A.default_target_dir(self.__class__.__name__)
        self.assertTrue(d.startswith(A.DEFAULT_MOVE_DIRNAME)
                        or os.path.basename(os.path.dirname(d)) == A.DEFAULT_MOVE_DIRNAME)


class TestPlan(TempCase):

    def test_items_and_totals(self):
        _write(os.path.join(self.root, "a.png"), b"12345")
        _write(os.path.join(self.root, "b.png"), b"123")
        plan = self._plan(["a.png", "b.png"])
        self.assertEqual(plan.files, 2)
        self.assertEqual(plan.bytes, 8)
        self.assertEqual(plan.blocked, [])
        self.assertEqual(plan.root, os.path.abspath(self.root))

    def test_duplicate_paths_are_collapsed(self):
        _write(os.path.join(self.root, "a.png"))
        plan = A.plan_from_paths([(os.path.join(self.root, "a.png"), 7)] * 3,
                                 root=self.root)
        self.assertEqual(plan.files, 1)

    def test_outside_root_is_blocked_not_deleted(self):
        outside = os.path.join(os.path.dirname(self.root), "outside.png")
        _write(outside)
        try:
            plan = A.plan_from_paths([(outside, 7)], root=self.root)
            self.assertEqual(plan.files, 0)
            self.assertEqual(len(plan.blocked), 1)
            self.assertIn("不在扫描根之下", plan.blocked[0])
        finally:
            os.remove(outside)

    def test_no_write_only_reason_field(self):
        # PlanItem.reason 只写不读，已移除；这条防它被加回来
        _write(os.path.join(self.root, "a.png"))
        item = self._plan(["a.png"]).items[0]
        self.assertFalse(hasattr(item, "reason"))


class TestExecuteDryRun(TempCase):

    def test_creates_nothing_but_the_manifest(self):
        _write(os.path.join(self.root, "a.png"))
        target = os.path.join(self.root, "_moved")
        res = A.execute(self._plan(["a.png"]),
                        A.Decision(mode=A.MODE_MOVE, target_dir=target),
                        manifest_dir=self.manifests, dry_run=True)
        # dry-run 连目标目录都不该建
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.exists(os.path.join(self.root, "a.png")))
        self.assertTrue(os.path.exists(res.manifest))

    def test_manifest_records_planned_totals(self):
        _write(os.path.join(self.root, "a.png"), b"12345")
        _write(os.path.join(self.root, "b.png"), b"123")
        res = A.execute(self._plan(["a.png", "b.png"]),
                        A.Decision(mode=A.MODE_MOVE, target_dir="x"),
                        manifest_dir=self.manifests, dry_run=True)
        m = _read_json(res.manifest)
        self.assertTrue(m["dry_run"])
        self.assertEqual(m["version"], A.MANIFEST_VERSION)
        self.assertEqual(m["totals"], {"files": 2, "bytes": 8,
                                       "failed": 0, "planned": 2})
        self.assertEqual({i["status"] for i in m["items"]}, {"dry-run"})
        self.assertEqual(res.ok, 2)
        self.assertEqual(res.bytes, 8)

    def test_move_mode_without_target_dir_raises(self):
        _write(os.path.join(self.root, "a.png"))
        with self.assertRaises(ValueError):
            A.execute(self._plan(["a.png"]),
                      A.Decision(mode=A.MODE_MOVE, target_dir=""),
                      manifest_dir=self.manifests)


class TestExecuteMove(TempCase):

    def _run(self, rels):
        target = os.path.join(self.root, "_moved")
        res = A.execute(self._plan(rels),
                        A.Decision(mode=A.MODE_MOVE, target_dir=target),
                        manifest_dir=self.manifests)
        return res, target

    def test_moves_and_preserves_structure(self):
        _write(os.path.join(self.root, "sub", "deep", "a.png"), b"12345")
        res, target = self._run([os.path.join("sub", "deep", "a.png")])
        self.assertEqual(res.ok, 1)
        self.assertEqual(res.failed, 0)
        self.assertEqual(res.bytes, 5)
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub", "deep", "a.png")))
        self.assertTrue(os.path.exists(os.path.join(target, "sub", "deep", "a.png")))

    def test_bytes_are_conserved(self):
        rels = []
        for i in range(5):
            rel = f"g{i}.png"
            _write(os.path.join(self.root, rel), b"z" * (10 * (i + 1)))
            rels.append(rel)
        want = sum(10 * (i + 1) for i in range(5))
        res, target = self._run(rels)
        got = 0
        for root, _dirs, names in os.walk(target):
            for n in names:
                got += os.path.getsize(os.path.join(root, n))
        self.assertEqual(got, want)
        self.assertEqual(res.bytes, want)

    def test_refuses_to_overwrite_existing_target(self):
        rel = "a.png"
        _write(os.path.join(self.root, rel), b"new")
        target = os.path.join(self.root, "_moved")
        _write(os.path.join(target, rel), b"old")
        res = A.execute(self._plan([rel]),
                        A.Decision(mode=A.MODE_MOVE, target_dir=target),
                        manifest_dir=self.manifests)
        self.assertEqual(res.failed, 1)
        self.assertEqual(res.ok, 0)
        self.assertIn("拒绝覆盖", res.items[0]["note"])
        # 原文件没被动过，目标也没被覆盖
        self.assertTrue(os.path.exists(os.path.join(self.root, rel)))
        self.assertEqual(_read(os.path.join(target, rel)), b"old")

    def test_missing_source_is_skipped_not_failed(self):
        rel = "a.png"
        _write(os.path.join(self.root, rel))
        plan = self._plan([rel])
        os.remove(os.path.join(self.root, rel))
        res = A.execute(plan, A.Decision(mode=A.MODE_MOVE,
                                         target_dir=os.path.join(self.root, "_m")),
                        manifest_dir=self.manifests)
        self.assertEqual(res.items[0]["status"], "skipped")
        self.assertEqual(res.failed, 0)

    def test_progress_callback_runs_once_per_item(self):
        rels = [f"{i}.png" for i in range(4)]
        for rel in rels:
            _write(os.path.join(self.root, rel))
        seen = []
        A.execute(self._plan(rels),
                  A.Decision(mode=A.MODE_MOVE,
                             target_dir=os.path.join(self.root, "_m")),
                  manifest_dir=self.manifests, on_progress=seen.append)
        self.assertEqual(len(seen), 4)
        self.assertEqual(sum(seen), 4)


class TestUndo(TempCase):

    def _move(self, rels):
        target = os.path.join(self.root, "_moved")
        res = A.execute(self._plan(rels),
                        A.Decision(mode=A.MODE_MOVE, target_dir=target),
                        manifest_dir=self.manifests)
        return res

    def test_restores_moved_files(self):
        rels = ["a.png", os.path.join("sub", "b.png")]
        for rel in rels:
            _write(os.path.join(self.root, rel), rel.encode())
        res = self._move(rels)
        out = A.undo(res.manifest)
        self.assertEqual(out.restored, 2)
        self.assertEqual(out.failed, 0)
        for rel in rels:
            p = os.path.join(self.root, rel)
            self.assertTrue(os.path.exists(p))
            self.assertEqual(_read(p), rel.encode())

    def test_is_idempotent(self):
        _write(os.path.join(self.root, "a.png"))
        res = self._move(["a.png"])
        A.undo(res.manifest)
        again = A.undo(res.manifest)
        self.assertEqual(again.restored, 0)
        self.assertIn("已还原过", "".join(again.notes))

    def test_conflict_is_reported_not_overwritten(self):
        _write(os.path.join(self.root, "a.png"), b"original")
        res = self._move(["a.png"])
        _write(os.path.join(self.root, "a.png"), b"intruder")
        out = A.undo(res.manifest)
        self.assertEqual(out.restored, 0)
        self.assertEqual(out.skipped, 1)
        self.assertIn("冲突", "".join(out.notes))
        self.assertEqual(_read(os.path.join(self.root, "a.png")), b"intruder")

    def test_dry_run_manifest_is_a_noop(self):
        _write(os.path.join(self.root, "a.png"))
        res = A.execute(self._plan(["a.png"]),
                        A.Decision(mode=A.MODE_MOVE, target_dir="x"),
                        manifest_dir=self.manifests, dry_run=True)
        out = A.undo(res.manifest)
        self.assertEqual(out.restored, 0)
        self.assertIn("预演", "".join(out.notes))


@unittest.skipUnless(sys.platform == "win32", "回收站路径只在 Windows 上有意义")
class TestRecycleRoundTrip(TempCase):
    """走一遍真实的回收站：execute → 文件进回收站 → undo 从 $I/$R 配对里捞回来。

    `_bin_lookup` + `build_bin_index` 是全项目最绕的一段代码（手解 `$I`
    二进制结构），不跑一遍真实往返没法证明它是对的。
    """

    def test_file_goes_to_recycle_bin_and_comes_back(self):
        rel = "recycle-me.png"
        src = _write(os.path.join(self.root, rel), b"recycle-payload")
        res = A.execute(self._plan([rel]),
                        A.Decision(mode=A.MODE_RECYCLE),
                        manifest_dir=self.manifests)
        if res.ok != 1:
            self.skipTest(f"本机 send2trash 不可用：{res.items[0].get('note')}")
        self.assertEqual(res.items[0]["dst"], "<回收站>")
        self.assertFalse(os.path.exists(src))

        out = A.undo(res.manifest)
        self.assertEqual(out.failed, 0, "".join(out.notes))
        self.assertEqual(out.restored, 1, "".join(out.notes))
        self.assertEqual(_read(src), b"recycle-payload")


@unittest.skipUnless(sys.platform == "win32", "8.3 短名只在 Windows 上有意义")
class TestShortNameNormalisation(unittest.TestCase):
    """`_norm_path` 必须把短名和长名归一到同一个键。

    回归用：曾经它只做 normcase/normpath，于是「用短名路径删的文件」
    在撤销时匹配不上回收站里记的长名路径，报「回收站里找不到」。
    """

    def test_short_and_long_forms_normalise_identically(self):
        import ctypes

        short = tempfile.gettempdir()
        buf = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetLongPathNameW(short, buf, 32768):
            self.skipTest("本机 temp 路径解析不出长名形式")
        long_form = buf.value
        if long_form.lower() == short.lower():
            self.skipTest("本机 temp 路径本来就是长名，复现不出这个问题")
        self.assertEqual(A._norm_path(short), A._norm_path(long_form))


class TestOpResult(unittest.TestCase):

    def test_describe_mentions_counts(self):
        r = A.OpResult(mode=A.MODE_MOVE, ok=3, failed=1, bytes=2 * 1024 ** 2)
        s = r.describe()
        self.assertIn("3", s)
        self.assertIn("1", s)
        self.assertIn("2.0 MB", s)


if __name__ == "__main__":
    unittest.main()
