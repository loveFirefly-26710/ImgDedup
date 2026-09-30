"""两级缓存：指纹复用库 + 缩略图磁盘缓存。

增量重扫能压到秒级，靠的就是这两个：

* 指纹库按 `(path, size, mtime_ns)` 复用上一轮算好的元数据 / 哈希 / 感知特征。
  文件没动过就完全不算：这是"重扫 < 10 秒"的主要来源。
* 缩略图缓存按同一把 key 存 WebP。感知阶段和 GUI 网格共用同一份产物，
  第二次打开界面缩略图是秒出的。

key 用 `(规范化绝对路径, size, mtime_ns)`：
  * 正确性：内容一变 mtime 就变，key 必变，不会读到过期缓存；
  * 代价：改名/移动会让缓存失效。这是已知取舍：想要"改名也不失效"
    需要换成 `(卷序列号, 文件索引)` 做 key，留到后续版本。
"""

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
    """指纹复用库。载入内存后用字典查，避免逐条查库。"""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._mem: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0

    # ---------------------------------------------------------- 载入
    def load(self) -> int:
        """一次性载入内存。1 万条约几十 MB，比逐条查库快一个量级。"""
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

    # ---------------------------------------------------------- 读
    def apply(self, entries: Iterable) -> int:
        """把命中的缓存回填到 entries 上，返回命中数。

        只填"缓存里有值"的字段；调用方据此跳过对应阶段（见 worker 的
        `todo = [e for e in ... if ...]`）。
        """
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

    # ---------------------------------------------------------- 写
    def save(self, entries: Iterable) -> int:
        now = time.time()
        n = 0
        rows = []
        for e in entries:
            if e.width <= 0 and e.quick is None and not e.feat_ok:
                continue                    # 什么都没算出来，不值得存
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

    def prune(self, keep_days: int = 90) -> int:
        cur = self.conn.execute("DELETE FROM fingerprints WHERE updated_at < ?",
                                (time.time() - keep_days * 86400,))
        self.conn.commit()
        return cur.rowcount or 0

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
    except Exception:  # noqa: BLE001
        return None


def _hist_from_blob(blob) -> object:
    if not blob:
        return None
    try:
        import numpy as np

        return np.frombuffer(blob, dtype=np.float32)
    except Exception:  # noqa: BLE001
        return None


# ================================================================ 缩略图

class ThumbStore:
    """缩略图磁盘缓存：`<root>/thumbs/<xx>/<key>-<size>.webp`。

    用 WebP（质量 80）：一张 256px 缩略图约 10–25 KB，
    1 万张也就 100–250 MB，换来的是重扫和界面加载都秒级。
    """

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
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            return False

    def thumb_for(self, entry, size_: Optional[int] = None):
        """取缩略图：先查磁盘缓存，未命中才解码并回写。

        返回 (RGB 数组 或 None, 来源标签)。
        """
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

    def size_on_disk(self) -> int:
        total = 0
        if not self.root.is_dir():
            return 0
        for dp, _dn, fn in os.walk(self.root):
            for f in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return total

    def clear(self) -> int:
        import shutil

        n = 0
        if self.root.is_dir():
            for dp, _dn, fn in os.walk(self.root):
                n += len(fn)
            shutil.rmtree(self.root, ignore_errors=True)
        return n


# ================================================================ 超大图降级

#: 超过这个像素量的图跳过感知哈希。
#:
#: 为什么不是"降低目标尺寸"：实测 233 MP 的 JPEG 缩略图要 210 ms，
#: 把目标从 256 降到 64 几乎不变：成本在熵解码（必须解完全部系数），
#: 不在输出尺寸。所以唯一有效的降级就是跳过。
#: 这些图仍然参与精确哈希与尺寸比对，只是不进感知层，UI 会标注。
DEFAULT_MAX_FEATURE_PIXELS = 100_000_000


def should_skip_features(pixels: int, limit: int = DEFAULT_MAX_FEATURE_PIXELS) -> bool:
    return limit > 0 and pixels > limit
