"""基础设施：内存闸门、计时、进度、单位格式化。"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


# ---------------------------------------------------------------- 单位格式化

def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def human_secs(s: float) -> str:
    if s < 1.0:
        return f"{s * 1000:.0f} ms"
    if s < 60.0:
        return f"{s:.2f} s"
    return f"{int(s // 60)}m {s % 60:.0f}s"


def human_rate(n: float) -> str:
    return f"{n:,.0f}/s"


# ---------------------------------------------------------------- 内存闸门

class MemGate:
    """按“预计解码字节数”发放额度的闸门。

    为什么按内存而不是按文件数限流：1 万张 200KB 的图和 1 万张 30MB 的图，
    同样的并发度意味着上百倍的内存差异。实测 47MP 图全解码 = 134 MB，
    12 并发就是 1.6 GB。
    """

    def __init__(self, budget_bytes: int):
        self.budget = max(1, int(budget_bytes))
        self._used = 0
        self._peak = 0
        self._cv = threading.Condition()

    @contextmanager
    def reserve(self, need: int):
        need = max(1, min(int(need), self.budget))
        with self._cv:
            while self._used + need > self.budget:
                self._cv.wait(timeout=0.25)
            self._used += need
            self._peak = max(self._peak, self._used)
        try:
            yield
        finally:
            with self._cv:
                self._used -= need
                self._cv.notify_all()

    @property
    def used(self) -> int:
        return self._used

    @property
    def peak(self) -> int:
        return self._peak


def default_memory_budget(cap_bytes: int = 2 * 1024**3) -> int:
    """内存预算 = min(物理内存 50%, 上限)。

    上限按项目约束收紧到 2GB：本项目的真实图库不含 100MB+ 单文件，
    实测最大单文件 15.9MB / 47MP，2GB 已有充足余量。
    """
    try:
        import psutil

        total = psutil.virtual_memory().total
    except Exception:
        total = 8 * 1024**3
    return int(min(total * 0.5, cap_bytes))


def estimate_decode_bytes(width: int, height: int, bands: int = 3, bytes_per_band: int = 1) -> int:
    return max(1, width * height * max(1, bands) * bytes_per_band)


# ---------------------------------------------------------------- 计时

@dataclass
class Timings:
    """分阶段计时。用于把“总耗时”拆开，避免只看一个总数看不出瓶颈。"""

    _t: dict[str, float] = field(default_factory=dict)

    def add(self, name: str, secs: float) -> None:
        self._t[name] = self._t.get(name, 0.0) + secs

    @contextmanager
    def stage(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - t0)

    def items(self):
        return sorted(self._t.items(), key=lambda kv: -kv[1])

    def total(self) -> float:
        return sum(self._t.values())


# ---------------------------------------------------------------- 进度

class Progress:
    """带节流的进度上报。

    收尾步骤也必须有独立进度，否则前端只能靠“99% 卡住”表达“还在算”。
    """

    def __init__(self, label: str, total: int, every: int = 500, quiet: bool = False):
        self.label = label
        self.total = max(0, total)
        self.every = max(1, every)
        self.quiet = quiet
        self.n = 0
        self.t0 = time.perf_counter()

    def tick(self, k: int = 1) -> None:
        self.n += k
        if self.quiet:
            return
        if self.n % self.every == 0 or self.n >= self.total:
            el = time.perf_counter() - self.t0
            pct = 100.0 * self.n / self.total if self.total else 100.0
            print(
                f"    [{self.label}] {self.n:,}/{self.total:,} "
                f"({pct:5.1f}%)  {human_rate(self.n / el if el else 0)}  {human_secs(el)}",
                flush=True,
            )

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.t0


# ---------------------------------------------------------------- CPU

def cpu_budget(ratio: float = 0.75) -> int:
    """工作线程数。默认不用满，给 GUI 与系统留余量。"""
    n = os.cpu_count() or 4
    return max(1, min(n, int(n * ratio)))
