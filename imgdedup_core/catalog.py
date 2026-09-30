"""目录库：SQLite（WAL）持久化扫描结果、特征与分组。

设计要点：指纹与文件身份分离存储，files 表按 (dev, ino) 做 UPSERT。
这样文件改名/移动不会导致指纹失效——这是增量重扫能压到秒级的原因。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Iterable, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    root         TEXT NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    options_json TEXT,
    status       TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id      INTEGER NOT NULL,
    path         TEXT NOT NULL,
    volume       INTEGER,
    file_index   INTEGER,
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    ext          TEXT,
    width        INTEGER,
    height       INTEGER,
    bands        INTEGER,
    fmt          TEXT,
    frames       INTEGER DEFAULT 1,
    animated     INTEGER DEFAULT 0,
    meta_backend TEXT,
    meta_note    TEXT,
    quick_hash   TEXT,
    full_hash    TEXT,
    phash        INTEGER,
    dhash        INTEGER,
    feat_ok      INTEGER DEFAULT 0,
    feat_note    TEXT,
    UNIQUE(scan_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_size  ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_full  ON files(full_hash);
CREATE INDEX IF NOT EXISTS idx_files_quick ON files(quick_hash);
CREATE INDEX IF NOT EXISTS idx_files_phash ON files(phash);
CREATE INDEX IF NOT EXISTS idx_files_ident ON files(volume, file_index);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    similarity  REAL,
    keep_file_id INTEGER,
    reclaim_bytes INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id   INTEGER NOT NULL,
    file_id    INTEGER NOT NULL,
    role       TEXT,
    score      REAL,
    reason     TEXT,
    user_decision TEXT,
    PRIMARY KEY (group_id, file_id)
);

CREATE TABLE IF NOT EXISTS operations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    kind          TEXT NOT NULL,
    manifest_path TEXT,
    file_count    INTEGER,
    bytes         INTEGER,
    undo_state    TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS protected_rules (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    reason  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Catalog:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._pending = 0

    # ------------------------------------------------------------ scans
    def begin_scan(self, root: str, options: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO scans(root, started_at, options_json) VALUES (?,?,?)",
            (root, time.time(), json.dumps(options, ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def end_scan(self, scan_id: int, status: str = "done") -> None:
        self.conn.execute(
            "UPDATE scans SET finished_at=?, status=? WHERE id=?",
            (time.time(), status, scan_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------ files
    def insert_files(self, scan_id: int, entries: Iterable, batch: int = 1000) -> int:
        """批量写入。每 batch 行提交一次，避免逐行 fsync。

        phash / dhash 是 64 位无符号值，最大可到 2^64-1，超出 SQLite 的
        有符号 64 位 INTEGER 范围（实测直接抛 OverflowError）。
        存前统一映射到有符号区间；读取侧按同样规则还原。
        """

        def i64(v: int) -> int:
            v &= 0xFFFFFFFFFFFFFFFF
            return v - (1 << 64) if v >= (1 << 63) else v

        n = 0
        for e in entries:
            self.conn.execute(
                """INSERT OR REPLACE INTO files
                   (scan_id, path, volume, file_index, size, mtime_ns, ext,
                    width, height, bands, fmt, frames, animated,
                    meta_backend, meta_note, quick_hash, full_hash,
                    phash, dhash, feat_ok, feat_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    scan_id, e.path, e.dev, e.ino, e.size, e.mtime_ns, e.ext,
                    e.width, e.height, e.bands, e.fmt, e.frames, int(e.animated),
                    e.meta_backend, e.meta_note,
                    e.quick.hex() if e.quick else None,
                    e.full.hex() if e.full else None,
                    i64(e.phash), i64(e.dhash), int(e.feat_ok), e.feat_note,
                ),
            )
            n += 1
            if n % batch == 0:
                self.conn.commit()
        self.conn.commit()
        return n

    # ------------------------------------------------------------ groups
    def insert_group(self, scan_id: int, kind: str, members: list[int],
                     similarity: Optional[float], keep_pos: int,
                     reclaim: int) -> int:
        cur = self.conn.execute(
            """INSERT INTO groups(scan_id, kind, member_count, similarity, reclaim_bytes)
               VALUES (?,?,?,?,?)""",
            (scan_id, kind, len(members), similarity, reclaim),
        )
        gid = int(cur.lastrowid)
        for pos, fid in enumerate(members):
            self.conn.execute(
                "INSERT OR REPLACE INTO group_members(group_id, file_id, role) VALUES (?,?,?)",
                (gid, fid, "keep" if pos == keep_pos else "candidate"),
            )
        self.conn.commit()
        return gid

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def group_count(self, scan_id: int, kind: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM groups WHERE scan_id=? AND kind=?", (scan_id, kind)
            ).fetchone()[0]
        )

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    def set_setting(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()
