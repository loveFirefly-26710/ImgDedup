"""特征层：两级精确哈希 + 感知哈希 + 颜色直方图。

哈希算法选择说明：
  * 用 XXH3-128（xxhash，本机已装）而不是 BLAKE3（未装）。
    对去重场景 128 位非加密哈希的碰撞概率可忽略（1 万张约 10^8/2^128）；
    而且删除前还会做一次逐字节确认（第三道闸），实际风险为零。
  * 两级策略：先用「头 64KB + 尾 64KB」的短指纹筛掉唯一文件，
    只对短指纹碰撞的候选算全量。这是"哈希可选、不 CPU 满载"的落地方式。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import xxhash

# 不要写 `import cv2`。cv2 已改为外置可选组件（86 MB，不随包分发），
# 顶层硬导入会在缺组件时让整个程序连窗口都建不出来。用惰性代理，
# 缺组件时抛的是带操作说明的 ImportError。见 `imgdedup_core.opencv`。
from .opencv import lazy_cv2 as cv2

QUICK_HEAD = 64 * 1024
QUICK_TAIL = 64 * 1024
CHUNK = 1 << 20


# ================================================================ 精确哈希

def quick_hash(path: str, head: int = QUICK_HEAD, tail: int = QUICK_TAIL) -> Optional[bytes]:
    """头尾短指纹。成本约 128KB/文件，相比读全文件（平均 0.65MB~5MB）省一个数量级。"""
    try:
        size = os.path.getsize(path)
        h = xxhash.xxh3_128()
        with open(path, "rb") as fh:
            h.update(fh.read(head))
            if size > head + tail:
                fh.seek(-tail, os.SEEK_END)
                h.update(fh.read(tail))
        h.update(size.to_bytes(8, "little"))
        return h.digest()
    except OSError:
        return None


def full_hash(path: str, chunk: int = CHUNK) -> Optional[bytes]:
    try:
        h = xxhash.xxh3_128()
        with open(path, "rb") as fh:
            while True:
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.digest()
    except OSError:
        return None


def byte_identical(a: str, b: str, chunk: int = CHUNK) -> bool:
    """逐字节确认。删除前的最后一道闸，把哈希碰撞风险降到零。"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ba, bb = fa.read(chunk), fb.read(chunk)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


# ================================================================ 感知特征

def _gray(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def phash(gray: np.ndarray) -> int:
    """pHash：DCT 低频 8x8，中位数阈值 → 64 bit。对缩放/重压缩/轻微调色鲁棒。"""
    g = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(g)[:8, :8].ravel().copy()
    d[0] = np.median(d[1:])  # 直流分量不携带结构信息，用中位数顶替
    bits = d > np.median(d)
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def dhash(gray: np.ndarray) -> int:
    """dHash：9x8 水平梯度 → 64 bit。对结构/边缘敏感，与 pHash 互补。"""
    g = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (g[:, 1:] > g[:, :-1]).ravel()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def hist_hsv(rgb: np.ndarray, bins: int = 16) -> np.ndarray:
    """HSV 三维直方图（3 x bins），归一化。捕捉整体色调分布。"""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h = cv2.calcHist([hsv], [0], None, [bins], [0, 180]).ravel()
    s = cv2.calcHist([hsv], [1], None, [bins], [0, 256]).ravel()
    v = cv2.calcHist([hsv], [2], None, [bins], [0, 256]).ravel()
    out = np.concatenate([h, s, v]).astype(np.float32)
    total = out.sum()
    return out / total if total > 0 else out


def hist_distance(a: np.ndarray, b: np.ndarray) -> float:
    """直方图相交距离的补：0 表示完全一致，1 表示完全不重叠。"""
    if a is None or b is None or a.size == 0 or b.size == 0:
        return 1.0
    return float(1.0 - np.minimum(a, b).sum())


@dataclass
class Features:
    phash: int = 0
    dhash: int = 0
    hist: Optional[np.ndarray] = None
    backend: str = ""
    note: str = ""
    ok: bool = False


def extract(rgb: Optional[np.ndarray], backend: str = "", note: str = "") -> Features:
    if rgb is None or rgb.size == 0:
        return Features(backend=backend, note=note or "解码失败", ok=False)
    try:
        g = _gray(rgb)
        return Features(phash=phash(g), dhash=dhash(g), hist=hist_hsv(rgb),
                        backend=backend, note=note, ok=True)
    except Exception as e:  # noqa: BLE001
        return Features(backend=backend, note=f"特征提取失败: {type(e).__name__}", ok=False)


# ================================================================ 向量化比较


def pairwise_hamming_block(query: np.ndarray, all_v: np.ndarray) -> np.ndarray:
    """query(block,) 对 all_v(n,) 的汉明距离矩阵，形状 (block, n)。

    分块是为了避免 N=1 万时出现 1 亿个元素的布尔矩阵。
    """
    x = np.bitwise_xor(query[:, None], all_v[None, :])
    return np.bitwise_count(x).astype(np.uint16)
