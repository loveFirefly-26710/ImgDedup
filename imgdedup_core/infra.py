"""与算法无关的基础设施：格式化、内存闸门、预算、计时与进度。

这一层不许依赖 Qt，也不许依赖 cv2 —— 它要在任何环境下都能导入。"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field



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



class MemGate:

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
    def peak(self) -> int:
        return self._peak


def default_memory_budget(cap_bytes: int = 2 * 1024**3) -> int:
    try:
        import psutil

        total = psutil.virtual_memory().total
    except Exception:
        total = 8 * 1024**3
    return int(min(total * 0.5, cap_bytes))


@dataclass
class Timings:

    _t: dict[str, float] = field(default_factory=dict)

    def add(self, name: str, secs: float) -> None:
        self._t[name] = self._t.get(name, 0.0) + secs

    def items(self):
        return sorted(self._t.items(), key=lambda kv: -kv[1])

    def total(self) -> float:
        return sum(self._t.values())



class Progress:

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



def cpu_budget(ratio: float = 0.75) -> int:
    n = os.cpu_count() or 4
    return max(1, min(n, int(n * ratio)))
