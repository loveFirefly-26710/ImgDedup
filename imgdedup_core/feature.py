"""指纹与感知特征。

哈希分两级：`quick_hash` 只读头尾各 64 KB（快，快速档用它），`full_hash` 读全文件。
感知层是 pHash / dHash / HSV 直方图三种互补特征，只用其中一种都会漏。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import xxhash

from .opencv import lazy_cv2 as cv2

QUICK_HEAD = 64 * 1024
QUICK_TAIL = 64 * 1024
CHUNK = 1 << 20



def quick_hash(path: str, head: int = QUICK_HEAD, tail: int = QUICK_TAIL) -> Optional[bytes]:
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


def _gray(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def phash(gray: np.ndarray) -> int:
    g = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(g)[:8, :8].ravel().copy()
    d[0] = np.median(d[1:])
    bits = d > np.median(d)
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def dhash(gray: np.ndarray) -> int:
    g = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (g[:, 1:] > g[:, :-1]).ravel()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def hist_hsv(rgb: np.ndarray, bins: int = 16) -> np.ndarray:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h = cv2.calcHist([hsv], [0], None, [bins], [0, 180]).ravel()
    s = cv2.calcHist([hsv], [1], None, [bins], [0, 256]).ravel()
    v = cv2.calcHist([hsv], [2], None, [bins], [0, 256]).ravel()
    out = np.concatenate([h, s, v]).astype(np.float32)
    total = out.sum()
    return out / total if total > 0 else out


def hist_distance(a: np.ndarray, b: np.ndarray) -> float:
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
    except Exception as e:
        return Features(backend=backend, note=f"特征提取失败: {type(e).__name__}", ok=False)




def pairwise_hamming_block(query: np.ndarray, all_v: np.ndarray) -> np.ndarray:
    x = np.bitwise_xor(query[:, None], all_v[None, :])
    return np.bitwise_count(x).astype(np.uint16)
