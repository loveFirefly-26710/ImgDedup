"""文件遍历与候选筛选。

`FileEntry` 是贯穿全流程的数据载体，字段按扫描阶段分组（遍历 → 元数据 →
短指纹 → 全量哈希 → 感知特征），后面每个阶段只填自己那一段。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Iterator

DEFAULT_EXCLUDE_DIRS = {
    ".scratch",
    ".imgdedup",
    ".git",
    ".svn",
    "__pycache__",
    "node_modules",
    "$RECYCLE.BIN",
    "System Volume Information",
}

DEFAULT_EXCLUDE_PREFIX = ("~$", "._")


@dataclass
class FileEntry:
    path: str
    size: int
    mtime_ns: int
    ext: str
    dev: int = 0
    ino: int = 0
    width: int = 0
    height: int = 0
    bands: int = 0
    fmt: str = ""
    meta_backend: str = ""
    meta_note: str = ""
    animated: bool = False
    frames: int = 1
    quick: bytes | None = None
    full: bytes | None = None
    phash: int = 0
    dhash: int = 0
    hist: object = None
    thumb_backend: str = ""
    feat_ok: bool = False
    feat_note: str = ""
    embed_ok: bool = False
    feat_cached: bool = False

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def identity(self) -> tuple[int, int]:
        return (self.dev, self.ino)


def walk(
    root: str,
    *,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    min_size: int = 1,
    follow_links: bool = False,
) -> Iterator[FileEntry]:
    ex = {d.lower() for d in exclude_dirs}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_links):
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in ex and not d.startswith(DEFAULT_EXCLUDE_PREFIX)
        ]
        for name in filenames:
            if name.startswith(DEFAULT_EXCLUDE_PREFIX):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full, follow_symlinks=follow_links)
            except OSError:
                continue
            if not os.path.isfile(full) or st.st_size < min_size:
                continue
            yield FileEntry(
                path=full,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                ext=os.path.splitext(name)[1].lower(),
                dev=getattr(st, "st_dev", 0),
                ino=getattr(st, "st_ino", 0),
            )


def find_hardlink_groups(entries: Iterable[FileEntry]) -> dict[tuple[int, int], list[str]]:
    seen: dict[tuple[int, int], list[str]] = {}
    for e in entries:
        if e.ino == 0:
            continue
        seen.setdefault(e.identity, []).append(e.path)
    return {k: v for k, v in seen.items() if len(v) > 1}


def bucket_by_size(entries: Iterable[FileEntry]) -> dict[int, list[FileEntry]]:
    buckets: dict[int, list[FileEntry]] = {}
    for e in entries:
        buckets.setdefault(e.size, []).append(e)
    return buckets
