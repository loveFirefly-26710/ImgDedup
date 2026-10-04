"""分组：并查集 + 分层合并。

精确 / 感知 / 语义三层依次加边。一个分量最终算哪一类，取它内部最弱的那层关系；
过大的分量单独隔离出来交人工确认，不参与批量删除。"""

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



class LayeredClusters:

    def __init__(self, n: int):
        self._n = n
        self._uf = UnionFind(n)
        self._kind: dict[int, str] = {}
        self._merges: dict[str, int] = {}

    def add_layer(self, groups, kind: str) -> int:
        effective = 0
        for g in groups:
            for k in range(1, len(g)):
                a, b = g[k - 1], g[k]
                if self._uf.find(a) == self._uf.find(b):
                    continue
                self._uf.union(a, b)
                self._kind[self._uf.find(a)] = kind
                effective += 1
        self._merges[kind] = self._merges.get(kind, 0) + effective
        return effective

    def merges(self, kind: str) -> int:
        return self._merges.get(kind, 0)

    def components(self, min_size: int = 2) -> list[tuple[str, list[int]]]:
        buckets: dict[int, list[int]] = {}
        for i in range(self._n):
            buckets.setdefault(self._uf.find(i), []).append(i)
        out: list[tuple[str, list[int]]] = []
        for root, members in buckets.items():
            if len(members) < min_size:
                continue
            out.append((self._kind.get(root, ""), sorted(members)))
        return out



def exact_groups(entries, *, hardlink_protected: set[int] | None = None):
    protected = hardlink_protected or set()
    buckets: dict[tuple[int, bytes], list[int]] = {}
    for i, e in enumerate(entries):
        if i in protected or e.full is None:
            continue
        buckets.setdefault((e.size, e.full), []).append(i)
    return [sorted(v) for v in buckets.values() if len(v) >= 2]



@dataclass
class PerceptualParams:
    phash_max: int = 6
    dhash_max: int = 12
    hist_max: float = 0.35
    block: int = 256


def perceptual_groups(entries, params: PerceptualParams | None = None,
                      *, exclude: set[int] | None = None):
    p = params or PerceptualParams()
    # 只有特征提取成功的项参与感知层。超大图会被 should_skip_features 拦下、
    # feat_ok 置 False，于是自然落选；exclude 是语义过合并后要隔离的下标。
    idx = [i for i, e in enumerate(entries)
           if e.feat_ok and (exclude is None or i not in exclude)]
    if len(idx) < 2:
        return []

    ph = np.array([entries[i].phash for i in idx], dtype=np.uint64)
    dh = np.array([entries[i].dhash for i in idx], dtype=np.uint64)
    hists = [entries[i].hist for i in idx]

    uf = UnionFind(len(idx))
    n = len(idx)
    # 两两比对是 O(n²)，按 block 切块算，避免一次开出 n×n 的矩阵
    for i0 in range(0, n, p.block):
        i1 = min(n, i0 + p.block)
        pd = F.pairwise_hamming_block(ph[i0:i1], ph)
        dd = F.pairwise_hamming_block(dh[i0:i1], dh)
        # 两道感知阈值都过，才有资格进候选
        mask = (pd <= p.phash_max) & (dd <= p.dhash_max)
        rows, cols = np.nonzero(mask)
        for r, c in zip(rows.tolist(), cols.tolist()):
            a, b = i0 + r, c
            if b <= a:
                continue
            # 直方图是最后一道闸：pHash/dHash 都只看结构，纯色块换来换去
            # 也能骗过它们，颜色分布再筛一次。hist_max >= 1.0 表示关掉这层。
            if p.hist_max < 1.0:
                if F.hist_distance(hists[a], hists[b]) > p.hist_max:
                    continue
            # 相似度按两个汉明距离之和折算（分母 128 = 64 位 pHash + 64 位 dHash）
            sim = 1.0 - (int(pd[r, c]) + int(dd[r, c])) / 128.0
            uf.union(a, b, sim)

    return [[idx[k] for k in g] for g in uf.groups(min_size=2)]



def recommend_keeper(entries, group: list[int], *, prefer_lossless: bool = True) -> int:
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
