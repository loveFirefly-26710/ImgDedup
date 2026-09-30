"""扫描层：目录遍历、文件身份、排除规则。

文件身份用 (st_dev, st_ino)。Windows 上 Python 的 os.stat 会把
st_dev 填成卷序列号、st_ino 填成文件索引，因此不需要 ctypes。
这一点很关键：硬链接的多个路径会得到同一个 (dev, ino)，
必须在去重前识别出来，否则会把硬链接当成"重复文件"删掉。
"""

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
    # 填充阶段
    width: int = 0
    height: int = 0
    bands: int = 0
    fmt: str = ""
    meta_backend: str = ""
    meta_note: str = ""
    animated: bool = False
    frames: int = 1
    # 特征阶段
    quick: bytes | None = None
    full: bytes | None = None
    phash: int = 0
    dhash: int = 0
    hist: object = None
    thumb_backend: str = ""
    feat_ok: bool = False
    feat_note: str = ""
    # 语义阶段（M6）
    embed: object = None
    embed_ok: bool = False
    embed_note: str = ""
    # 缓存命中标记（M4）：命中即意味着对应阶段可以整体跳过
    cached: bool = False
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
    """并行友好的目录遍历。跳过噪声目录与 0 字节文件。"""
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
    """找出共享同一 inode 的路径（硬链接）。

    这些不是重复文件：删掉其中一个不释放空间，还会破坏用户
    可能有意建立的链接结构。默认保护，不计入可删除集合。
    """
    seen: dict[tuple[int, int], list[str]] = {}
    for e in entries:
        if e.ino == 0:
            continue
        seen.setdefault(e.identity, []).append(e.path)
    return {k: v for k, v in seen.items() if len(v) > 1}


def bucket_by_size(entries: Iterable[FileEntry]) -> dict[int, list[FileEntry]]:
    """按文件大小分桶。

    大小不同 → 字节必然不同，可以直接排除，这是零成本的第一级筛选。
    实测在 E:\\1\\test 上把 3203 个文件压到 1799 个候选。
    """
    buckets: dict[int, list[FileEntry]] = {}
    for e in entries:
        buckets.setdefault(e.size, []).append(e)
    return buckets
