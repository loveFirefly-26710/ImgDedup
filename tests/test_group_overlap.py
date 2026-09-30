"""每个文件只能出现在"最强"的那一类分组里。

## 为什么需要这组测试

字节相同的图，其 pHash 与 dHash 距离必然是 0、直方图距离也必然是 0
（实测：两张相同的 JPEG，三项距离全是 0）。所以**精确重复的成员必然满足
感知判定**，而 `perceptual_groups` 拿到的是全部文件 —— 于是同一批文件同时
出现在「精确重复」和「近似重复」两类里。

在 10 张真图的图库上实测到的后果：

  * 底栏「可释放」把同一批文件算两遍：显示 12.69 MB，真实 6.55 MB（**1.94 倍**）；
  * `planned()` 里出现重复路径，删除对话框说"将删除 6 个"，而只有 4 个不同文件。

修法是在**源头**排除（精确组成员不进感知分组、感知组成员不进语义分组），
而不是在下游去重数字 —— 下游去重会让底栏（去重后）和网格（仍重复显示）
互相矛盾，而且会把"上游又漏了排除"这种回归掩盖掉。

## 关于 CI 的一个缺口

行为级的那两条要 cv2 才能产生感知特征，而 cv2 是外置组件、CI 上不装
（`requirements*.txt` 里没有它），所以在 CI 里会 skip。
为此另加了一条**源码级**的接线断言，见 `test_worker_passes_exclusion_to_perceptual` ——
它弱，但它是 CI 上唯一能挡住"有人把 `exclude=` 删掉"的东西。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgdedup_core import action as A  # noqa: E402
from imgdedup_core import embed as EMB  # noqa: E402
from imgdedup_core import group as G  # noqa: E402
from imgdedup_core import scan as S  # noqa: E402

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 三项特征都相同 —— 模拟"字节完全相同的两张图"
SAME = (0b1010_1010, 0b0101_0101, np.full(48, 0.5, dtype=np.float32))


def _entry(path: str, feats=SAME, size: int = 100) -> S.FileEntry:
    e = S.FileEntry(path=path, size=size, mtime_ns=0, ext=".png")
    e.width = e.height = 10
    e.fmt = "PNG"
    e.feat_ok = True
    e.phash, e.dhash, e.hist = feats[0], feats[1], feats[2].copy()
    return e


class TestPerceptualExclude(unittest.TestCase):
    def test_exclude_removes_files_from_grouping(self):
        entries = [_entry(f"/x/{i}.png") for i in range(4)]

        # 不排除：四张特征全同，聚成一组
        groups = G.perceptual_groups(entries)
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(sorted(groups[0]), [0, 1, 2, 3])

        # 排除前三张（它们在精确组里）：只剩第 4 张，凑不成组
        self.assertEqual(G.perceptual_groups(entries, exclude={0, 1, 2}), [])

        # 只排除一张：剩下三张仍成一组，且不含被排除的那个
        groups = G.perceptual_groups(entries, exclude={0})
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(sorted(groups[0]), [1, 2, 3])

    def test_exclude_of_nothing_is_the_same_as_no_exclude(self):
        """空集合与 None 必须等价 —— 否则 worker 里那个"还没分组"的初值会改变行为。"""
        entries = [_entry(f"/x/{i}.png") for i in range(3)]
        self.assertEqual(G.perceptual_groups(entries, exclude=set()),
                         G.perceptual_groups(entries))


class TestCosineExclude(unittest.TestCase):
    def _embedded(self, n: int):
        entries = [_entry(f"/x/{i}.png") for i in range(n)]
        for e in entries:
            e.embed_ok = True
        return entries

    def test_exclude_removes_files_from_grouping(self):
        entries = self._embedded(4)
        v = np.array([[1, 0], [1, 0], [1, 0], [0, 1]], dtype=np.float32)

        groups, _ = EMB.cosine_groups(entries, v, threshold=0.97)
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(sorted(groups[0]), [0, 1, 2])

        groups, _ = EMB.cosine_groups(entries, v, threshold=0.97,
                                     exclude={0, 1, 2})
        self.assertEqual(groups, [])

    def test_exclude_keeps_index_vector_alignment(self):
        """排除中间几个时，返回的分组必须仍指向正确的文件。

        `vectors` 的第 k 行对应 `idx` 的第 k 项。只滤 `idx` 不滤 `vectors`，
        分组会**静默指到别的文件身上**（不报错，只是结果错）。
        所以这里构造一个"错位会得到不同答案"的输入：

            v = [A, A, B, A, B]   （A 与 B 正交）
            排除下标 0 之后
              正确（同步过滤）：idx=[1,2,3,4] → 组 {1,3} 与 {2,4}
              错位（只滤 idx）：v 的前 4 行是 [A,A,B,A] → 组 {1,2,4}
        """
        entries = self._embedded(5)
        v = np.array([[1, 0], [1, 0], [0, 1], [1, 0], [0, 1]],
                     dtype=np.float32)

        groups, _ = EMB.cosine_groups(entries, v, threshold=0.99,
                                     exclude={0})
        got = sorted(sorted(g) for g in groups)
        self.assertEqual(got, [[1, 3], [2, 4]],
                         "分组指到了错误的文件上 —— 下标和向量没有同步过滤")


class TestWorkerPassesExclusion(unittest.TestCase):
    """CI 上没有 cv2，行为级断言会 skip，所以这里补一条源码级的接线断言。

    它只能证明"调用点写了 `exclude=`"，证明不了行为。行为那部分见
    `TestNoOverlapEndToEnd`，那条在有 cv2 的环境里跑。
    """

    def test_worker_passes_exclusion_to_perceptual(self):
        with open(os.path.join(PROJ, "imgdedup_ui", "worker.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        # 去掉整行注释再断言：否则注释里写一句 exclude= 就能骗过这条
        body = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertRegex(
            body, r"perceptual_groups\(entries,\s*exclude=",
            "worker 没把精确组的成员排除出感知分组 —— 重叠会回来")
        self.assertRegex(
            body, r"_semantic\(entries, gate, workers,\s*exclude=",
            "worker 没把已分组的成员排除出语义分组")


class TestNoOverlapEndToEnd(unittest.TestCase):
    """真造图、真起 worker，验"每个文件只进一类"。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _scan(self, root: str):
        from PySide6.QtCore import QEventLoop, QTimer

        from imgdedup_ui.worker import ScanWorker

        loop = QEventLoop()
        holder: dict = {}
        w = ScanWorker(root, mode="balanced", threshold=0.97)
        w.finished_ok.connect(lambda rows, meta: (
            holder.update(rows=rows, meta=meta), loop.quit()))
        w.failed.connect(lambda e: (holder.update(error=e), loop.quit()))
        QTimer.singleShot(120_000, loop.quit)
        w.start()
        loop.exec()
        w.wait(5000)
        return holder

    def test_identical_files_land_in_exactly_one_kind(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没有 Pillow")

        from imgdedup_core import opencv as OPENCV

        if not OPENCV.is_available():
            self.skipTest("没有 cv2（外置组件，CI 上不装）—— 产生不了感知特征")

        with tempfile.TemporaryDirectory() as d:
            # 3 张字节完全相同 + 2 张同色的缩小版。
            # 五张在感知上是同一张，所以"不排除"时它们会聚成一组 5 张，
            # 而精确组是其中 3 张 —— 重叠就发生在这里。
            for name in ("a.png", "b.png", "c.png"):
                Image.new("RGB", (80, 60), (10, 120, 200)).save(
                    os.path.join(d, name))
            # 颜色差一点点：同尺寸的纯色图如果颜色也一样，存出来是**字节相同**的，
            # 那它们自己就构成第二个精确组，这条测试的前提就变了。
            for name, rgb in (("small1.png", (10, 120, 200)),
                              ("small2.png", (12, 121, 201))):
                Image.new("RGB", (40, 30), rgb).save(os.path.join(d, name))

            holder = self._scan(d)
            self.assertNotIn("error", holder,
                             f"扫描崩了：{holder.get('error')}")
            rows = holder.get("rows") or []

            # 精确组必须找到那 3 张
            exact = [r for r in rows if r.kind == "exact"]
            self.assertEqual(len(exact), 1, f"精确组应为 1 个，实际 {len(exact)}")
            self.assertEqual(exact[0].count, 3)

            # 感知组必须存在（否则这条测试是空转的）
            perc = [r for r in rows if r.kind == "perceptual"]
            self.assertTrue(perc, "没有产出感知组 —— 这条测试失去意义")
            self.assertEqual(sum(r.count for r in perc), 2,
                             "感知组里应该只剩那两张缩小版")

            # 核心不变量：同一路径不能出现在两个 kind 里
            by_path: dict[str, set] = {}
            for r in rows:
                for it in r.items:
                    by_path.setdefault(os.path.normcase(it.path), set()).add(
                        r.kind)
            dup = {p: ks for p, ks in by_path.items() if len(ks) > 1}
            self.assertEqual(dup, {}, f"这些文件同时出现在两类里：{dup}")

            # 派生不变量：待删清单不能有重复路径（重复会让对话框的计数虚高）
            from imgdedup_ui import model as M

            gm = M.GroupModel()
            gm.set_rows(rows)
            gm.apply_auto_to_all()
            planned = gm.planned()
            paths = [p for p, _ in planned]
            self.assertEqual(len(paths), len(set(paths)),
                             "planned() 里有重复路径")

            # 底栏那个数必须等于"真正会删掉的字节"
            self.assertEqual(gm.stats()["reclaim"],
                             sum(dict(planned).values()),
                             "底栏『可释放』与实际待删字节不一致")


class TestSkippedIsNotFailure(unittest.TestCase):
    """文件在"计划"与"执行"之间被外部删掉时，不算失败。

    注意 `plan_from_paths` **已经对路径去重**（`action.py` 的 `seen` 集合），
    所以这条分支不是由重复路径触发的，而是"文件真的不在了"。
    """

    def test_missing_source_is_skipped_not_counted_as_failed(self):
        with tempfile.TemporaryDirectory() as d:
            gone = os.path.join(d, "gone.jpg")
            plan = A.plan_from_paths([(gone, 123)], root=d, kind="ui")
            self.assertEqual(len(plan.items), 1, "不存在的路径应当照样进计划")

            manifest_dir = os.path.join(d, "manifests")
            res = A.execute(plan, A.Decision(mode=A.MODE_RECYCLE),
                            manifest_dir=manifest_dir)
            self.assertEqual(res.items[0]["status"], "skipped")
            self.assertEqual(res.failed, 0,
                             "跳过的项被计成了失败 —— 状态栏会报出假失败")
            self.assertEqual(res.ok, 0)

    def test_plan_from_paths_dedupes(self):
        """这条是上面那条的前提：重复路径在这里就被去掉了。

        所以 GUI 那条路径（`window._delete_paths_now`）不会重复删同一个文件；
        真正虚高的是对话框里那个按未去重清单算出来的计数。
        """
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "a.jpg")
            pathlib.Path(f).write_bytes(b"x")
            plan = A.plan_from_paths([(f, 1), (f, 1), (f, 1)], root=d, kind="ui")
            self.assertEqual(len(plan.items), 1, "重复路径没被去掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
