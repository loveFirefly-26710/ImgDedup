"""路径 A（用户删除重复图片）的测试。

重点不是"能跑"，而是不变量成立：
  * 每组至少保留 1 张；
  * 不在扫描根之下的路径被拦下；
  * 精确组里非逐字节相同的候选被整体拦下；
  * 硬链接不参与删除；
  * 撤销能完整还原，冲突时报告而不覆盖；
  * 与路径 B 物理隔离（本模块不得引用 scratch 的真删能力）。

回收站相关的用例会把文件放进用户的真实回收站，所以 tearDown 里
必须按前缀清扫干净，否则每跑一次测试就往回收站里扔垃圾。
"""

import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgdedup_core import action as A  # noqa: E402
from imgdedup_core import scratch as SC  # noqa: E402
from imgdedup_core.scan import FileEntry  # noqa: E402

BOX = None


def setUpModule():
    global BOX
    BOX = SC.scratch_root() / "_action_tests"
    shutil.rmtree(BOX, ignore_errors=True)
    BOX.mkdir(parents=True, exist_ok=True)


def tearDownModule():
    shutil.rmtree(BOX, ignore_errors=True)
    # 回收站里可能留着本模块产生的条目，按前缀清掉
    SC.sweep_recycle_bin([BOX])


def make_entry(path, fmt="PNG"):
    st = os.stat(path)
    return FileEntry(path=str(path), size=st.st_size, mtime_ns=st.st_mtime_ns,
                     ext=os.path.splitext(path)[1].lower(),
                     dev=getattr(st, "st_dev", 0), ino=getattr(st, "st_ino", 0),
                     width=800, height=600, fmt=fmt)


class TestPlanning(unittest.TestCase):
    def setUp(self):
        self.root = BOX / "plan_lib"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.entries = []

        # 组 0：3 张逐字节相同
        payload = b"identical-payload" * 128
        for k in range(3):
            p = self.root / f"same_{k}.bin"
            p.write_bytes(payload)
            self.entries.append(make_entry(p))

        # 组 1：2 张内容不同
        for k in range(2):
            p = self.root / f"diff_{k}.bin"
            p.write_bytes(b"different" * 128 + bytes([k]))
            self.entries.append(make_entry(p))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_exact_group_plans_two_victims(self):
        plan = A.plan_deletions(self.entries, [[0, 1, 2]], root=str(self.root),
                                kind="exact")
        self.assertEqual(plan.files, 2, "3 张里应当删 2 张、留 1 张")
        self.assertEqual(plan.blocked, [])
        self.assertNotIn(self.entries[plan.items[0].group].path, [])  # 结构可用

    def test_at_least_one_keeper_per_group(self):
        """不变量 1：每组至少保留 1 张。"""
        plan = A.plan_deletions(self.entries, [[0, 1, 2]], root=str(self.root),
                                kind="exact")
        victims = {i.path for i in plan.items}
        survivors = [e.path for e in self.entries[:3] if e.path not in victims]
        self.assertEqual(len(survivors), 1, "必须有且仅有 1 张存活")

    def test_non_identical_candidates_are_blocked(self):
        """不变量 4：精确组里非逐字节相同的候选整体拦下（第三道闸）。"""
        plan = A.plan_deletions(self.entries, [[3, 4]], root=str(self.root),
                                kind="exact")
        self.assertEqual(plan.files, 0)
        self.assertTrue(any("逐字节" in b for b in plan.blocked), plan.blocked)

    def test_perceptual_kind_skips_byte_check(self):
        plan = A.plan_deletions(self.entries, [[3, 4]], root=str(self.root),
                                kind="perceptual")
        self.assertEqual(plan.files, 1, "感知组不做逐字节确认，应当放行 1 张")

    def test_paths_outside_root_are_blocked(self):
        """不变量 2：不在扫描根之下的路径必须拦下。"""
        outside = BOX / "outside.bin"
        outside.write_bytes(b"identical-payload" * 128)
        entries = list(self.entries[:3]) + [make_entry(outside)]
        # 显式指定保留 0 号，确保 3 号（根之外）落在待删侧
        plan = A.plan_deletions(entries, [[0, 1, 3]], root=str(self.root),
                                kind="perceptual", user_keep={0: 0})
        self.assertTrue(any("不在扫描根之下" in b for b in plan.blocked),
                        plan.blocked)
        self.assertNotIn(str(outside), [i.path for i in plan.items])

    def test_hardlink_protected_are_excluded(self):
        """不变量 3：硬链接不参与删除。"""
        plan = A.plan_deletions(self.entries, [[0, 1, 2]], root=str(self.root),
                                kind="exact", hardlink_protected={1})
        self.assertNotIn(str(self.entries[1].path), [i.path for i in plan.items],
                         "受保护的硬链接不得进入待删清单")
        # 排除受保护项后只剩 2 个成员 -> 保留 1 张、删 1 张
        self.assertEqual(plan.files, 1)

    def test_group_with_only_protected_members_is_skipped(self):
        plan = A.plan_deletions(self.entries, [[0, 1]], root=str(self.root),
                                kind="exact", hardlink_protected={0, 1})
        self.assertEqual(plan.files, 0)
        self.assertTrue(plan.blocked)

    def test_user_keep_overrides_recommendation(self):
        plan = A.plan_deletions(self.entries, [[0, 1, 2]], root=str(self.root),
                                kind="exact", user_keep={0: 2})
        victims = {i.path for i in plan.items}
        self.assertNotIn(str(self.entries[2].path), victims, "用户指定保留的不能删")
        self.assertEqual(len(victims), 2)

    def test_under_root_normalises_paths(self):
        self.assertTrue(A._under_root(str(self.root / "a.bin"), str(self.root)))
        self.assertTrue(A._under_root(str(self.root), str(self.root)))
        self.assertFalse(A._under_root(str(BOX / "elsewhere.bin"), str(self.root)))


class TestDecisionPersistence(unittest.TestCase):
    def test_roundtrip_and_reset(self):
        store = {}

        def setter(k, v):
            store[k] = v

        def getter(k, d):
            return store.get(k, d)

        self.assertIsNone(A.load_decision(getter), "没记住时应当返回 None（要弹窗）")

        A.save_decision(setter, A.Decision(mode=A.MODE_MOVE,
                                           target_dir="D:/removed", remember=True))
        d = A.load_decision(getter)
        self.assertIsNotNone(d)
        self.assertEqual(d.mode, A.MODE_MOVE)
        self.assertEqual(d.target_dir, "D:/removed")

        A.clear_decision(setter)
        self.assertIsNone(A.load_decision(getter), "「重新启用提示」后必须重新弹窗")

    def test_remember_false_is_not_honoured(self):
        store = {A.SETTING_REMEMBER: False, A.SETTING_MODE: A.MODE_RECYCLE}
        self.assertIsNone(A.load_decision(lambda k, d: store.get(k, d)))

    def test_invalid_mode_is_ignored(self):
        store = {A.SETTING_REMEMBER: True, A.SETTING_MODE: "explode"}
        self.assertIsNone(A.load_decision(lambda k, d: store.get(k, d)))


class TestExecuteAndUndo(unittest.TestCase):
    def setUp(self):
        self.root = BOX / "exec_lib"
        self.dest = BOX / "exec_dest"
        self.manifests = BOX / "manifests"
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.dest, ignore_errors=True)
        shutil.rmtree(self.manifests, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)

        self.entries = []
        payload = b"exec-payload" * 256
        for k in range(3):
            p = self.root / "sub" / f"dup_{k}.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
            self.entries.append(make_entry(p))

    def tearDown(self):
        for d in (self.root, self.dest, self.manifests):
            shutil.rmtree(d, ignore_errors=True)
        SC.sweep_recycle_bin([BOX])

    def _plan(self):
        return A.plan_deletions(self.entries, [[0, 1, 2]], root=str(self.root),
                                kind="exact")

    def test_move_mode_moves_and_undo_restores(self):
        plan = self._plan()
        self.assertEqual(plan.files, 2)

        res = A.execute(plan, A.Decision(mode=A.MODE_MOVE, target_dir=str(self.dest)),
                        manifest_dir=str(self.manifests))
        self.assertEqual(res.ok, 2, res.describe())
        self.assertEqual(res.failed, 0)
        self.assertTrue(os.path.exists(res.manifest))

        # 原路径已空，目标目录里保留了子目录结构
        remaining = list(self.root.rglob("*.bin"))
        self.assertEqual(len(remaining), 1, "应当只剩保留的那 1 张")
        moved = list(self.dest.rglob("*.bin"))
        self.assertEqual(len(moved), 2)
        self.assertTrue((self.dest / "sub").is_dir(), "应当保留原目录结构")

        undo = A.undo(res.manifest)
        self.assertEqual(undo.restored, 2, undo.notes)
        self.assertEqual(undo.failed, 0, undo.notes)
        self.assertEqual(len(list(self.root.rglob("*.bin"))), 3)

    def test_undo_is_idempotent(self):
        plan = self._plan()
        res = A.execute(plan, A.Decision(mode=A.MODE_MOVE, target_dir=str(self.dest)),
                        manifest_dir=str(self.manifests))
        A.undo(res.manifest)
        second = A.undo(res.manifest)
        self.assertEqual(second.restored, 0)
        self.assertTrue(any("已还原过" in n for n in second.notes), second.notes)

    def test_undo_reports_conflict_instead_of_overwriting(self):
        """还原时原路径已有文件 → 必须报告冲突，绝不覆盖。"""
        plan = self._plan()
        res = A.execute(plan, A.Decision(mode=A.MODE_MOVE, target_dir=str(self.dest)),
                        manifest_dir=str(self.manifests))

        victim = res.items[0]["src"]
        with open(victim, "wb") as fh:
            fh.write(b"NEW CONTENT MUST SURVIVE")

        undo = A.undo(res.manifest)
        with open(victim, "rb") as fh:
            self.assertEqual(fh.read(), b"NEW CONTENT MUST SURVIVE",
                             "冲突时覆盖了用户的文件！")
        self.assertGreaterEqual(undo.skipped, 1)
        self.assertTrue(any("冲突" in n for n in undo.notes), undo.notes)

    def test_dry_run_writes_manifest_but_touches_nothing(self):
        plan = self._plan()
        res = A.execute(plan, A.Decision(mode=A.MODE_MOVE, target_dir=str(self.dest)),
                        manifest_dir=str(self.manifests), dry_run=True)
        self.assertEqual(len(list(self.root.rglob("*.bin"))), 3, "预演不能动文件")
        self.assertFalse(os.path.exists(self.dest))
        with open(res.manifest, encoding="utf-8") as fh:
            m = json.load(fh)
        self.assertTrue(m["dry_run"])
        self.assertEqual(m["totals"]["planned"], 2)

    def test_recycle_mode_does_not_truly_delete(self):
        """回收站模式：文件必须还在（在回收站里），绝不是真删。"""
        plan = self._plan()
        victim = plan.items[0].path
        res = A.execute(plan, A.Decision(mode=A.MODE_RECYCLE),
                        manifest_dir=str(self.manifests))
        self.assertEqual(res.failed, 0, res.describe())
        self.assertFalse(os.path.exists(victim), "原路径应当已空")

        rp = A.find_in_recycle_bin(victim)
        self.assertIsNotNone(rp, "文件没有进回收站 —— 路径 A 绝不允许真删")

        undo = A.undo(res.manifest)
        self.assertEqual(undo.restored, 2, undo.notes)
        self.assertEqual(len(list(self.root.rglob("*.bin"))), 3)

    def test_execute_refuses_move_without_target(self):
        plan = self._plan()
        with self.assertRaises(ValueError):
            A.execute(plan, A.Decision(mode=A.MODE_MOVE, target_dir=""),
                      manifest_dir=str(self.manifests))


class TestPathIsolation(unittest.TestCase):
    """路径 A 与路径 B 必须物理隔离，且路径 A 绝不丢数据。

    比起查源码文本，行为断言更硬：无论哪种模式，字节数都必须守恒 ——
    数据要么在原处、要么在目标目录、要么在回收站，绝不能凭空消失。
    """

    def test_action_does_not_import_scratch(self):
        with open(A.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("from .scratch", src)
        self.assertNotIn("import scratch", src)
        self.assertNotIn("purge", src, "路径 A 不得出现真删通道")
        self.assertNotIn("shutil.rmtree(", src, "路径 A 不得有整目录删除")

    def test_bytes_are_conserved_in_both_modes(self):
        for mode in (A.MODE_RECYCLE, A.MODE_MOVE):
            with self.subTest(mode=mode):
                root = BOX / f"iso_{mode}"
                dest = BOX / f"iso_dest_{mode}"
                mf = BOX / f"iso_mf_{mode}"
                for d in (root, dest, mf):
                    shutil.rmtree(d, ignore_errors=True)
                root.mkdir(parents=True, exist_ok=True)
                mf.mkdir(parents=True, exist_ok=True)

                payload = b"iso-payload" * 512
                entries = []
                for k in range(3):
                    p = root / f"d{k}.bin"
                    p.write_bytes(payload)
                    entries.append(make_entry(p))
                total = sum(e.size for e in entries)

                plan = A.plan_deletions(entries, [[0, 1, 2]], root=str(root),
                                        kind="exact")
                res = A.execute(plan, A.Decision(mode=mode, target_dir=str(dest)),
                                manifest_dir=str(mf))
                self.assertEqual(res.failed, 0, res.describe())

                recovered = 0
                for e in entries:
                    if os.path.exists(e.path):
                        recovered += os.path.getsize(e.path)
                        continue
                    if mode == A.MODE_MOVE:
                        rel = os.path.relpath(e.path, str(root))
                        d = dest / rel
                        self.assertTrue(d.exists(), f"{e.path} 在目标目录里找不到")
                        recovered += d.stat().st_size
                    else:
                        rp = A.find_in_recycle_bin(e.path)
                        self.assertIsNotNone(
                            rp, f"{e.path} 既不在原处也不在回收站 —— 被真删了")
                        recovered += rp.stat().st_size

                self.assertEqual(recovered, total, "字节数不守恒，有数据丢失")

                for d in (root, dest, mf):
                    shutil.rmtree(d, ignore_errors=True)
                SC.sweep_recycle_bin([BOX])


if __name__ == "__main__":
    unittest.main(verbosity=2)
