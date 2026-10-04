"""扫描缓存：指纹库（SQLite）+ 缩略图文件缓存。

指纹以「路径 + 大小 + mtime」为键，文件一改就自然失效，不需要额外的失效逻辑。"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints (
    key           TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    ext           TEXT,
    width         INTEGER DEFAULT 0,
    height        INTEGER DEFAULT 0,
    bands         INTEGER DEFAULT 0,
    fmt           TEXT,
    meta_backend  TEXT,
    meta_note     TEXT,
    quick_hash    TEXT,
    full_hash     TEXT,
    phash         INTEGER,
    dhash         INTEGER,
    hist          BLOB,
    feat_ok       INTEGER DEFAULT 0,
    feat_note     TEXT,
    thumb_backend TEXT,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_fp_updated ON fingerprints(updated_at);
"""


def entry_key(path: str, size: int, mtime_ns: int) -> str:
    norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return f"{norm}|{size}|{mtime_ns}"


def _i64(v: int) -> int:
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= (1 << 63) else v


def _u64(v: int) -> int:
    return v & 0xFFFFFFFFFFFFFFFF


class FingerprintStore:

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._mem: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0

    def load(self) -> int:
        self._mem.clear()
        cur = self.conn.execute(
            "SELECT key,ext,width,height,bands,fmt,meta_backend,meta_note,"
            "quick_hash,full_hash,phash,dhash,hist,feat_ok,feat_note,thumb_backend "
            "FROM fingerprints")
        for r in cur:
            self._mem[r[0]] = {
                "ext": r[1], "width": r[2] or 0, "height": r[3] or 0, "bands": r[4] or 0,
                "fmt": r[5], "meta_backend": r[6], "meta_note": r[7],
                "quick_hash": r[8], "full_hash": r[9],
                "phash": r[10], "dhash": r[11], "hist": r[12],
                "feat_ok": bool(r[13]), "feat_note": r[14], "thumb_backend": r[15],
            }
        return len(self._mem)

    def __len__(self) -> int:
        return len(self._mem)

    def apply(self, entries: Iterable) -> int:
        hit = 0
        for e in entries:
            rec = self._mem.get(entry_key(e.path, e.size, e.mtime_ns))
            if rec is None:
                self.misses += 1
                continue
            if not e.ext and rec["ext"]:
                e.ext = rec["ext"]
            if rec["width"] and not e.width:
                e.width, e.height, e.bands = rec["width"], rec["height"], rec["bands"]
                e.fmt = rec["fmt"] or e.fmt
                e.meta_backend = rec["meta_backend"] or ""
                e.meta_note = rec["meta_note"] or ""
            if rec["quick_hash"] and e.quick is None:
                e.quick = bytes.fromhex(rec["quick_hash"])
            if rec["full_hash"] and e.full is None:
                e.full = bytes.fromhex(rec["full_hash"])
            if rec["hist"] is not None:
                e.phash = _u64(rec["phash"] or 0)
                e.dhash = _u64(rec["dhash"] or 0)
                e.hist = _hist_from_blob(rec["hist"])
                e.feat_ok = rec["feat_ok"]
                e.thumb_backend = rec["thumb_backend"] or ""
                e.feat_note = rec["feat_note"] or "（缓存）"
                e.feat_cached = True
            e.cached = True
            hit += 1
        self.hits += hit
        return hit

    def save(self, entries: Iterable) -> int:
        now = time.time()
        n = 0
        rows = []
        for e in entries:
            if e.width <= 0 and e.quick is None and not e.feat_ok:
                continue
            rows.append((
                entry_key(e.path, e.size, e.mtime_ns), e.path, e.size, e.mtime_ns,
                e.ext, e.width, e.height, e.bands, e.fmt, e.meta_backend, e.meta_note,
                e.quick.hex() if e.quick else None,
                e.full.hex() if e.full else None,
                _i64(e.phash), _i64(e.dhash), _hist_to_blob(e.hist),
                int(e.feat_ok), e.feat_note or "", e.thumb_backend or "", now,
            ))
            n += 1
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO fingerprints VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.conn.commit()
        return n

    def stats(self) -> dict:
        n = self.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        return {"rows": n, "loaded": len(self._mem), "hits": self.hits,
                "misses": self.misses}

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()


def _hist_to_blob(hist) -> Optional[bytes]:
    if hist is None:
        return None
    try:
        import numpy as np

        return np.asarray(hist, dtype=np.float32).tobytes()
    except Exception:
        return None


def _hist_from_blob(blob) -> object:
    if not blob:
        return None
    try:
        import numpy as np

        return np.frombuffer(blob, dtype=np.float32)
    except Exception:
        return None



class ThumbStore:

    def __init__(self, root: str, *, size: int = 256, quality: int = 80):
        self.root = Path(root)
        self.size = size
        self.quality = quality
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def key(self, path: str, size_: int, mtime_ns: int) -> str:
        norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        import hashlib

        h = hashlib.blake2b(f"{norm}|{size_}|{mtime_ns}".encode("utf-8", "replace"),
                            digest_size=10).hexdigest()
        return f"{h}-{size_}"

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.webp"

    def get(self, key: str):
        p = self.path_for(key)
        if not p.exists():
            self.misses += 1
            return None
        try:
            import numpy as np

            from .opencv import lazy_cv2 as cv2

            buf = np.fromfile(str(p), dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is None:
                self.misses += 1
                return None
            self.hits += 1
            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        except Exception:
            self.misses += 1
            return None

    def put(self, key: str, rgb) -> bool:
        if rgb is None:
            return False
        try:
            import numpy as np

            from .opencv import lazy_cv2 as cv2

            p = self.path_for(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            bgr = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)
            ok, buf = cv2.imencode(".webp", bgr,
                                   [int(cv2.IMWRITE_WEBP_QUALITY), self.quality])
            if not ok:
                return False
            tmp = p.with_suffix(".webp.part")
            buf.tofile(str(tmp))
            os.replace(tmp, p)
            self.writes += 1
            return True
        except Exception:
            return False

    def thumb_for(self, entry, size_: Optional[int] = None):
        from . import decode as D

        size_ = size_ or self.size
        key = self.key(entry.path, size_, entry.mtime_ns)
        arr = self.get(key)
        if arr is not None:
            return arr, "cache"
        arr, backend = D.load_small(entry.path, size_, entry.ext, entry.pixels)
        if arr is not None:
            self.put(key, arr)
        return arr, backend

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}

DEFAULT_MAX_FEATURE_PIXELS = 100_000_000


def should_skip_features(pixels: int, limit: int = DEFAULT_MAX_FEATURE_PIXELS) -> bool:
    return limit > 0 and pixels > limit
