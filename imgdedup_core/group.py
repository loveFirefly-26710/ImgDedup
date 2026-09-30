"""分组层：并查集聚类 + 相似度矩阵（分块计算）。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import feature as F


class UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))
        self.rank = [0] * n
        self.edges: dict[int, float] = {}

    def find(self, x: int) -> int:
        p = self.p
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a: int, b: int, score: float = 0.0) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        self.edges[min(a, b)] = score

    def groups(self, min_size: int = 2) -> list[list[int]]:
        buckets: dict[int, list[int]] = {}
        for i in range(len(self.p)):
            buckets.setdefault(self.find(i), []).append(i)
        return [sorted(v) for v in buckets.values() if len(v) >= min_size]


# ================================================================ 精确分组

def exact_groups(entries, *, hardlink_protected: set[int] | None = None):
    """按 (size, full_hash) 分组。

    hardlink_protected: 同一 inode 的条目索引集合，不参与重复判定。
    """
    protected = hardlink_protected or set()
    buckets: dict[tuple[int, bytes], list[int]] = {}
    for i, e in enumerate(entries):
        if i in protected or e.full is None:
            continue
        buckets.setdefault((e.size, e.full), []).append(i)
    return [sorted(v) for v in buckets.values() if len(v) >= 2]


# ================================================================ 感知分组

@dataclass
class PerceptualParams:
    phash_max: int = 6
    dhash_max: int = 12
    hist_max: float = 0.35
    block: int = 256


def perceptual_groups(entries, params: PerceptualParams | None = None,
                      *, exclude: set[int] | None = None):
    """按 pHash/dHash 汉明距离 + 直方图距离聚类。

    `exclude` 里的下标不参与聚类。调用方用它把"已经在更强的分组里出现过"的
    文件排除掉：字节相同的图，其 pHash 与 dHash 距离必然是 0、直方图距离也
    必然是 0，所以精确重复的成员必然满足感知判定 —— 不排除的话同一批文件
    会同时出现在「精确重复」和「近似重复」两类里，用户按两类各删一遍，
    而"可释放空间"被算了两遍。

    复杂度：暴力两两比较。N=1 万时约 5x10^7 对，
    向量化 popcount 后约 0.2-0.5 秒——不需要 HNSW/BK-tree。
    分块计算是为了避免一次分配 N^2 的矩阵（1 万 → 1 亿元素）。
    """
    p = params or PerceptualParams()
    idx = [i for i, e in enumerate(entries)
           if e.feat_ok and (exclude is None or i not in exclude)]
    if len(idx) < 2:
        return []

    ph = np.array([entries[i].phash for i in idx], dtype=np.uint64)
    dh = np.array([entries[i].dhash for i in idx], dtype=np.uint64)
    hists = [entries[i].hist for i in idx]

    uf = UnionFind(len(idx))
    n = len(idx)
    for i0 in range(0, n, p.block):
        i1 = min(n, i0 + p.block)
        pd = F.pairwise_hamming_block(ph[i0:i1], ph)
        dd = F.pairwise_hamming_block(dh[i0:i1], dh)
        mask = (pd <= p.phash_max) & (dd <= p.dhash_max)
        rows, cols = np.nonzero(mask)
        for r, c in zip(rows.tolist(), cols.tolist()):
            a, b = i0 + r, c
            if b <= a:
                continue
            if p.hist_max < 1.0:
                if F.hist_distance(hists[a], hists[b]) > p.hist_max:
                    continue
            sim = 1.0 - (int(pd[r, c]) + int(dd[r, c])) / 128.0
            uf.union(a, b, sim)

    return [[idx[k] for k in g] for g in uf.groups(min_size=2)]


# ================================================================ 推荐保留

def recommend_keeper(entries, group: list[int], *, prefer_lossless: bool = True) -> int:
    """给一组选推荐保留项，返回索引。

    评分顺序：分辨率 → 文件大小 → 无损格式优先 → 修改时间更新。
    结果必须能在 UI 上给出理由，且用户可一键反向。
    """
    lossless = {"PNG", "TIFF", "BMP", "PSD", "WEBP"}

    def score(i: int):
        e = entries[i]
        fmt = (e.fmt or "").upper()
        lossless_rank = 1 if (prefer_lossless and fmt in lossless) else 0
        return (e.pixels, e.size, lossless_rank, e.mtime_ns)

    return max(group, key=score)


def keeper_reason(entries, group: list[int], keep: int) -> str:
    e = entries[keep]
    parts = []
    max_px = max(entries[i].pixels for i in group)
    if e.pixels == max_px and max_px > 0:
        parts.append("分辨率最高")
    if e.size == max(entries[i].size for i in group):
        parts.append("文件最大")
    if (e.fmt or "").upper() in {"PNG", "TIFF", "BMP", "PSD"}:
        parts.append("无损格式")
    if not parts:
        parts.append("综合评分最高")
    return "、".join(parts)
