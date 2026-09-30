"""扫描线程的暂停/取消语义 + 报告导出。

暂停这块值得单独钉住：`cancel()` 如果不同时解除暂停，工作线程会永远卡在
`_pause.wait()` 里：界面看起来"取消成功"，进程却退不掉。
"""

import os
import shutil
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgdedup_core import scratch as SC  # noqa: E402
from imgdedup_ui.model import DELETE, KEEP, GroupModel, GroupRow, ItemRow  # noqa: E402

BOX = None


def setUpModule():
    global BOX
    from PySide6.QtWidgets import QApplication

    global _APP
    _APP = QApplication.instance() or QApplication([])
    BOX = SC.scratch_root() / "_worker_tests"
    shutil.rmtree(BOX, ignore_errors=True)
    BOX.mkdir(parents=True, exist_ok=True)


def tearDownModule():
    shutil.rmtree(BOX, ignore_errors=True)


def make_rows():
    m = GroupModel()
    rows = []
    r1 = GroupRow(kind="exact", label="字节完全相同", keeps={0}, items=[
        ItemRow(path="C:/lib/a/dup1.jpg", size=1000, width=800, height=600, fmt="JPEG"),
        ItemRow(path="C:/lib/b/dup2.jpg", size=900, width=800, height=600, fmt="JPEG"),
        ItemRow(path="C:/lib/c/dup3.jpg", size=800, width=640, height=480, fmt="JPEG"),
    ])
    r1.auto_decide()
    rows.append(r1)
    r2 = GroupRow(kind="cluster", label="过合并", keeps={0}, oversized=True, items=[
        ItemRow(path="C:/lib/x/a.png", size=500, width=100, height=100, fmt="PNG"),
        ItemRow(path="C:/lib/x/b.png", size=400, width=100, height=100, fmt="PNG"),
    ])
    r2.auto_decide()
    rows.append(r2)
    m.set_rows(rows)
    return m, rows


class TestPauseSemantics(unittest.TestCase):
    def test_toggle_roundtrip(self):
        from imgdedup_ui.worker import ScanWorker

        w = ScanWorker("x")
        self.assertFalse(w.paused)
        self.assertTrue(w.toggle_pause())
        self.assertTrue(w.paused)
        self.assertFalse(w.toggle_pause())
        self.assertFalse(w.paused)

    def test_cancel_releases_pause(self):
        """取消必须解除暂停，否则工作线程会一直卡在 _pause.wait() 里。"""
        from imgdedup_ui.worker import ScanWorker

        w = ScanWorker("x")
        w.pause()
        self.assertTrue(w.paused)
        w.cancel()
        self.assertFalse(w.paused, "cancel 之后仍处于暂停 —— 工作线程会卡死")

    def test_gated_blocks_while_paused_then_runs_after_resume(self):
        from imgdedup_core.infra import MemGate

        from imgdedup_ui.worker import ScanWorker

        w = ScanWorker("x")
        w.pause()
        gate = MemGate(1024 * 1024)
        done = []
        t = threading.Thread(target=lambda: w._gated(gate, 1024, done.append, 1),
                             daemon=True)
        t.start()
        time.sleep(0.35)
        self.assertEqual(done, [], "暂停期间任务仍被执行了")
        w.resume()
        t.join(timeout=3)
        self.assertEqual(done, [1], "恢复后任务没有继续执行")

    def test_cancelled_gated_does_not_run(self):
        from imgdedup_core.infra import MemGate

        from imgdedup_ui.worker import ScanWorker

        w = ScanWorker("x")
        w.cancel()
        gate = MemGate(1024 * 1024)
        done = []
        r = w._gated(gate, 1024, done.append, 1)
        self.assertIsNone(r)
        self.assertEqual(done, [], "已取消却仍执行了任务")


class TestReportExport(unittest.TestCase):
    def setUp(self):
        self.dir = BOX / "report"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.model, self.rows = make_rows()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_csv_one_row_per_file(self):
        from imgdedup_ui import report as R

        p = str(self.dir / "r.csv")
        n = R.export_csv(self.rows, p)
        self.assertEqual(n, 5, "2 组共 5 个文件，应当导出 5 行")
        text = open(p, encoding="utf-8-sig").read()
        self.assertIn("保留", text)
        self.assertIn("待删", text)
        self.assertEqual(len(text.strip().splitlines()), 6, "5 行数据 + 1 行表头")

    def test_html_has_stats_and_all_paths(self):
        from imgdedup_ui import report as R

        p = str(self.dir / "r.html")
        n = R.export_html(self.rows, {"root": "C:/lib", "files": 5}, p)
        self.assertEqual(n, 2, "应当导出 2 组")
        html = open(p, encoding="utf-8").read()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("C:/lib", html)
        for path in ("dup1.jpg", "dup2.jpg", "dup3.jpg"):
            self.assertIn(path, html)

    def test_html_notes_oversized_cluster(self):
        from imgdedup_ui import report as R

        p = str(self.dir / "r2.html")
        R.export_html(self.rows, {}, p)
        html = open(p, encoding="utf-8").read()
        self.assertIn("不参与批量删除", html)

    def test_html_escapes_paths(self):
        """路径里的尖括号必须转义，否则报告本身就是个 XSS/坏 HTML。"""
        from imgdedup_ui import report as R

        rows = [GroupRow(kind="exact", label="x", keeps={0}, items=[
            ItemRow(path="C:/a/<script>alert(1)</script>.jpg", size=10),
            ItemRow(path="C:/b/normal.jpg", size=9),
        ])]
        rows[0].auto_decide()
        p = str(self.dir / "esc.html")
        R.export_html(rows, {}, p)
        html = open(p, encoding="utf-8").read()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_records_include_decision(self):
        from imgdedup_ui import report as R

        recs = R.rows_to_records(self.rows)
        self.assertEqual(len(recs), 5)
        keepers = [r for r in recs if r["is_keeper"]]
        self.assertEqual(len(keepers), 2, "每组恰好 1 个保留项")
        self.assertTrue(all(r["decision"] == KEEP for r in keepers))
        dels = [r for r in recs if r["decision"] == DELETE]
        self.assertEqual(len(dels), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
