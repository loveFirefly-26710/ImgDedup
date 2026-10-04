"""`imgdedup_core/catalog.py`：SQLite 目录库（扫描结果与设置）。"""

import os
import sqlite3
import tempfile
import unittest

from imgdedup_core import catalog as C
from imgdedup_core.scan import FileEntry


def _entry(path: str, size: int = 10) -> FileEntry:
    e = FileEntry(path=path, size=size, mtime_ns=100, ext=".png", dev=1, ino=2)
    e.width, e.height, e.bands = 100, 50, 3
    e.fmt = "PNG"
    e.quick = b"\x01\x02"
    e.full = b"\x03\x04"
    e.phash, e.dhash = 5, 6
    e.feat_ok = True
    return e


class TempCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imgdedup_catalog_")
        self.db_path = os.path.join(self.tmp.name, "sub", "catalog.db")
        self.db = C.Catalog(self.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class TestSchema(TempCase):

    def test_creates_parent_directory(self):
        self.assertTrue(os.path.exists(self.db_path))

    def test_all_declared_tables_exist(self):
        names = {r[0] for r in self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("scans", "files", "groups", "group_members",
                  "operations", "protected_rules", "settings"):
            with self.subTest(table=t):
                self.assertIn(t, names)

    def test_wal_mode_is_on(self):
        mode = self.db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_has_no_count_method(self):
        # count() 零引用，已移除；这条防它被加回来
        self.assertFalse(hasattr(C.Catalog, "count"))


class TestScanLifecycle(TempCase):

    def test_begin_and_end_scan(self):
        sid = self.db.begin_scan("C:/imgs", {"mode": "balanced"})
        self.assertIsInstance(sid, int)
        self.db.end_scan(sid)
        row = self.db.conn.execute(
            "SELECT root, options_json, status, finished_at FROM scans WHERE id=?",
            (sid,)).fetchone()
        self.assertEqual(row[0], "C:/imgs")
        self.assertIn("balanced", row[1])
        self.assertEqual(row[2], "done")
        self.assertIsNotNone(row[3])

    def test_end_scan_can_record_a_failure(self):
        sid = self.db.begin_scan("C:/imgs", {})
        self.db.end_scan(sid, status="failed")
        self.assertEqual(
            self.db.conn.execute("SELECT status FROM scans WHERE id=?",
                                 (sid,)).fetchone()[0], "failed")


class TestInsertFiles(TempCase):

    def test_returns_count_and_roundtrips_hashes(self):
        sid = self.db.begin_scan("C:/imgs", {})
        n = self.db.insert_files(sid, [_entry("C:/imgs/a.png"),
                                       _entry("C:/imgs/b.png", size=20)])
        self.assertEqual(n, 2)
        row = self.db.conn.execute(
            "SELECT path, size, quick_hash, full_hash, phash, feat_ok "
            "FROM files WHERE path=? ORDER BY path", ("C:/imgs/a.png",)).fetchone()
        self.assertEqual(row[0], "C:/imgs/a.png")
        self.assertEqual(row[1], 10)
        self.assertEqual(row[2], b"\x01\x02".hex())
        self.assertEqual(row[3], b"\x03\x04".hex())
        self.assertEqual(row[4], 5)
        self.assertEqual(row[5], 1)

    def test_same_path_twice_does_not_duplicate(self):
        sid = self.db.begin_scan("C:/imgs", {})
        self.db.insert_files(sid, [_entry("C:/imgs/a.png")])
        self.db.insert_files(sid, [_entry("C:/imgs/a.png")])
        n = self.db.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        self.assertEqual(n, 1)

    def test_empty_input_is_fine(self):
        sid = self.db.begin_scan("C:/imgs", {})
        self.assertEqual(self.db.insert_files(sid, []), 0)


class TestInsertGroup(TempCase):

    def test_marks_keeper_by_position(self):
        sid = self.db.begin_scan("C:/imgs", {})
        self.db.insert_files(sid, [_entry(f"C:/imgs/{i}.png") for i in range(3)])
        fids = [r[0] for r in self.db.conn.execute(
            "SELECT id FROM files ORDER BY path")]
        gid = self.db.insert_group(sid, "exact", fids, 1.0, keep_pos=1, reclaim=30)
        self.assertIsInstance(gid, int)
        rows = self.db.conn.execute(
            "SELECT file_id, role FROM group_members WHERE group_id=? ORDER BY file_id",
            (gid,)).fetchall()
        self.assertEqual([r[1] for r in rows], ["candidate", "keep", "candidate"])

    def test_group_header_counts_members(self):
        sid = self.db.begin_scan("C:/imgs", {})
        gid = self.db.insert_group(sid, "semantic", [1, 2, 3, 4], 0.98,
                                   keep_pos=0, reclaim=100)
        row = self.db.conn.execute(
            "SELECT kind, member_count, similarity, reclaim_bytes "
            "FROM groups WHERE id=?", (gid,)).fetchone()
        self.assertEqual(row, ("semantic", 4, 0.98, 100))


class TestSettings(TempCase):

    def test_json_roundtrip(self):
        self.db.set_setting("delete.mode", "move")
        self.db.set_setting("delete.remember", True)
        self.db.set_setting("nested", {"a": [1, 2]})
        self.assertEqual(self.db.get_setting("delete.mode"), "move")
        self.assertIs(self.db.get_setting("delete.remember"), True)
        self.assertEqual(self.db.get_setting("nested"), {"a": [1, 2]})

    def test_missing_key_returns_default(self):
        self.assertIsNone(self.db.get_setting("nope"))
        self.assertEqual(self.db.get_setting("nope", "fallback"), "fallback")

    def test_setting_survives_reopen(self):
        self.db.set_setting("k", "v")
        # 再开一个连接读同一个文件 —— 不要关 self.db，tearDown 还要用它
        again = C.Catalog(self.db_path)
        try:
            self.assertEqual(again.get_setting("k"), "v")
        finally:
            again.close()

    def test_non_json_value_comes_back_as_raw_string(self):
        self.db.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                             ("raw", "not-json"))
        self.db.conn.commit()
        self.assertEqual(self.db.get_setting("raw"), "not-json")


class TestClose(unittest.TestCase):

    def test_operations_after_close_raise(self):
        tmp = tempfile.TemporaryDirectory(prefix="imgdedup_catalog_")
        try:
            db = C.Catalog(os.path.join(tmp.name, "c.db"))
            db.set_setting("a", 1)
            db.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                db.get_setting("a")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
