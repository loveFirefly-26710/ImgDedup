"""`imgdedup_ui/worker.py` 的档位表与暂停语义，以及 `report.py` 的导出。

不启动线程、不真扫描：这里验的是「表是对的」和「导出的东西是对的」。
"""

import os
import tempfile
import unittest

from imgdedup_core.scan import FileEntry

from imgdedup_ui import model as M
from imgdedup_ui import report as R
from imgdedup_ui import worker as Wk


def _rows():
    es = []
    for i in range(4):
        e = FileEntry(path=f"C:/imgs/g{i}.png", size=100 + i, mtime_ns=0, ext=".png")
        e.width, e.height, e.fmt = 10 + i, 10 + i, "PNG"
        es.append(e)
    return (M.build_rows(es, [[0, 1]], "exact")
            + M.build_rows(es, [[2, 3]], "cluster", oversized=True))


class TestModeTable(unittest.TestCase):

    def test_three_modes_in_order(self):
        self.assertEqual(Wk.MODE_ORDER, ("fast", "balanced", "deep"))
        self.assertEqual(set(Wk.MODES), set(Wk.MODE_ORDER))

    def test_each_mode_has_the_same_fields(self):
        want = {"name", "tag", "brief", "detects", "misses", "cost", "index", "kinds"}
        for key, m in Wk.MODES.items():
            with self.subTest(mode=key):
                self.assertEqual(set(m), want)

    def test_index_matches_the_order_table(self):
        for i, key in enumerate(Wk.MODE_ORDER):
            with self.subTest(mode=key):
                self.assertEqual(Wk.MODES[key]["index"], i)

    def test_kinds_are_nested(self):
        """档位越高覆盖的类别越多 —— 快速档只出精确重复。"""
        fast = set(Wk.MODES["fast"]["kinds"])
        balanced = set(Wk.MODES["balanced"]["kinds"])
        deep = set(Wk.MODES["deep"]["kinds"])
        self.assertTrue(fast < balanced < deep)
        self.assertEqual(fast, {"exact"})
        self.assertIn("semantic", deep)

    def test_mode_at(self):
        self.assertEqual(Wk.mode_at(0), "fast")
        self.assertEqual(Wk.mode_at(2), "deep")
        self.assertEqual(Wk.mode_at(99), "balanced")
        self.assertEqual(Wk.mode_at(-1), "balanced")

    def test_mode_info_falls_back(self):
        self.assertIs(Wk.mode_info("deep"), Wk.MODES["deep"])
        self.assertIs(Wk.mode_info("nope"), Wk.MODES["balanced"])


class TestScanWorkerState(unittest.TestCase):

    def _worker(self, **kw):
        return Wk.ScanWorker("C:/imgs", **kw)

    def test_deep_mode_implies_semantic(self):
        w = self._worker(mode="deep")
        self.assertTrue(w.semantic)
        self.assertEqual(w.mode, "deep")

    def test_semantic_flag_promotes_the_mode(self):
        w = self._worker(semantic=True, mode="balanced")
        self.assertEqual(w.mode, "deep")
        self.assertTrue(w.semantic)

    def test_fast_mode_is_not_semantic(self):
        w = self._worker(mode="fast")
        self.assertFalse(w.semantic)

    def test_toggle_pause_roundtrip(self):
        w = self._worker()
        self.assertFalse(w.paused)
        self.assertTrue(w.toggle_pause(), "第一次调用应当是「切到暂停」")
        self.assertTrue(w.paused)
        self.assertFalse(w.toggle_pause(), "第二次调用应当是「切回运行」")
        self.assertFalse(w.paused)

    def test_cancel_sets_the_flag_and_releases_the_pause(self):
        w = self._worker()
        w.toggle_pause()
        w.cancel()
        self.assertTrue(w._cancel)
        self.assertFalse(w.paused, "cancel 必须同时解除暂停，否则线程卡在 wait() 上")

    def test_has_no_redundant_pause_resume(self):
        # 与 toggle_pause 重复且无人调用，已移除
        self.assertFalse(hasattr(Wk.ScanWorker, "pause"))
        self.assertFalse(hasattr(Wk.ScanWorker, "resume"))

    def test_has_no_stage_order_constant(self):
        # 零引用，已移除
        self.assertFalse(hasattr(Wk.ScanWorker, "STAGE_ORDER"))


class TestKindText(unittest.TestCase):

    def test_uses_the_report_label(self):
        row = _rows()[0]
        row.label = "exact"
        self.assertEqual(R.kind_text(row), R.KIND_LABEL["exact"])

    def test_appends_a_meaningful_extra_label(self):
        row = _rows()[0]
        row.label = "含 2 组字节重复"
        self.assertEqual(R.kind_text(row),
                         f"{R.KIND_LABEL['exact']}（含 2 组字节重复）")

    def test_oversized_rows_never_get_an_extra_label(self):
        row = _rows()[1]
        row.label = "含 2 组字节重复"
        self.assertTrue(row.oversized)
        self.assertEqual(R.kind_text(row), R.KIND_LABEL["cluster"])


class TestRecords(unittest.TestCase):

    def test_one_record_per_item(self):
        recs = R.rows_to_records(_rows())
        self.assertEqual(len(recs), 4)
        self.assertEqual(set(recs[0]), {
            "group", "kind", "kind_label", "oversized", "decision",
            "is_keeper", "path", "name", "size", "width", "height", "fmt"})

    def test_group_index_and_name(self):
        recs = R.rows_to_records(_rows())
        self.assertEqual({r["group"] for r in recs}, {0, 1})
        self.assertTrue(all(r["name"].endswith(".png") for r in recs))

    def test_exactly_one_keeper_per_group(self):
        recs = R.rows_to_records(_rows())
        for g in {r["group"] for r in recs}:
            keepers = [r for r in recs if r["group"] == g and r["is_keeper"]]
            with self.subTest(group=g):
                self.assertEqual(len(keepers), 1)


class TestExport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imgdedup_report_")
        self.root = self.tmp.name
        self.rows = _rows()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _bytes(path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    @staticmethod
    def _text(path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_csv_has_a_bom_and_a_header(self):
        path = os.path.join(self.root, "r.csv")
        n = R.export_csv(self.rows, path)
        self.assertEqual(n, 4)
        raw = self._bytes(path)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 要带 BOM，Excel 才不乱码")
        text = raw.decode("utf-8-sig")
        self.assertIn("组号", text.splitlines()[0])
        self.assertEqual(len(text.strip().splitlines()), 5)   # 表头 + 4 行

    def test_csv_creates_missing_directories(self):
        path = os.path.join(self.root, "deep", "nested", "r.csv")
        R.export_csv(self.rows, path)
        self.assertTrue(os.path.exists(path))

    def test_html_contains_counts_and_rows(self):
        path = os.path.join(self.root, "r.html")
        n = R.export_html(self.rows, {"root": "C:/imgs", "files": 123}, path)
        self.assertEqual(n, 2)
        html = self._text(path)
        self.assertIn("123", html)
        self.assertIn("ImgDedup 去重报告", html)
        self.assertEqual(html.count("<tr"), 4 + 1)             # 表头 + 4 行

    def test_html_escapes_paths(self):
        rows = _rows()
        rows[0].items[0].path = "C:/imgs/<script>alert(1)</script>.png"
        path = os.path.join(self.root, "esc.html")
        R.export_html(rows, {}, path)
        html = self._text(path)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_html_notes_oversized_rows(self):
        path = os.path.join(self.root, "over.html")
        R.export_html(self.rows, {}, path)
        self.assertIn("过合并", self._text(path))

    def test_html_without_oversized_rows_has_no_note(self):
        path = os.path.join(self.root, "plain.html")
        R.export_html([_rows()[0]], {}, path)
        self.assertNotIn("不参与批量删除", self._text(path))

    def test_html_tolerates_missing_meta(self):
        path = os.path.join(self.root, "nometa.html")
        R.export_html(self.rows, None, path)
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
