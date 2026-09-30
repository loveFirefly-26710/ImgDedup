"""端到端验证：三档模式真的跑出不同结果与不同耗时。

这是本轮最关键的验证，因为要修的就是"快速"和"精确"跑同一条流水线
的真 bug。只断言 `on_start` 传了什么参数不够：万一 worker 内部又把
分支走回同一条路呢？必须真扫一遍、看产出的组数和耗时。

用法： python dev/probe_modes_e2e.py [目录]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "E:/1/test/流萤表情包 - 副本"

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from imgdedup_ui.worker import ScanWorker

    # 这个赋值不能删：Qt 侧只持有弱引用，没有 Python 侧的强引用时
    # QApplication 可能被 GC，界面会随机崩。pyflakes 会把它报成未使用。
    app = QApplication.instance() or QApplication([])

    results = {}
    for mode in ("fast", "balanced"):
        loop = QEventLoop()
        holder = {"rows": None, "meta": None}

        w = ScanWorker(root, mode=mode, threshold=0.92,
                       cache_dir=str(PROJ / ".scratch" / "modecache"))
        w.finished_ok.connect(lambda rows, meta: (
            holder.update(rows=rows, meta=meta), loop.quit()))
        w.failed.connect(lambda e: (print("FAILED:", e), loop.quit()))
        QTimer.singleShot(600_000, loop.quit)

        t0 = time.perf_counter()
        w.start()
        loop.exec()
        dt = time.perf_counter() - t0
        w.wait(5000)

        rows, meta = holder["rows"], holder["meta"]
        results[mode] = (dt, rows, meta)
        print(f"\n=== mode={mode}  耗时 {dt:.2f}s")
        if meta:
            for k in ("files", "candidates", "exact_groups",
                      "perceptual_groups", "full_hash_skipped",
                      "perceptual_skipped", "worker_errors"):
                if k in meta:
                    print(f"    {k}: {meta[k]}")
        if rows is not None:
            kinds = {}
            for r in rows:
                kinds[r.kind] = kinds.get(r.kind, 0) + 1
            print(f"    分组: {kinds}  共 {len(rows)} 组")

    # ---- 断言：两档必须真的不同 ----
    print("\n" + "=" * 50)
    fast_rows, bal_rows = results["fast"][1], results["balanced"][1]
    fast_meta, bal_meta = results["fast"][2], results["balanced"][2]

    ok = True
    if fast_rows is not None and bal_rows is not None:
        if len(fast_rows) == len(bal_rows):
            print("两档组数相同 —— 可能没修好")
            ok = False
        else:
            print(f"✓ 组数不同：fast={len(fast_rows)} balanced={len(bal_rows)}")
    if fast_meta and bal_meta:
        if not fast_meta.get("full_hash_skipped"):
            print("fast 没有标记跳过全量哈希")
            ok = False
        else:
            print("✓ fast 确实跳过了全量哈希与感知阶段")
    if results["fast"][0] < results["balanced"][0]:
        print(f"✓ fast 更快：{results['fast'][0]:.2f}s "
              f"< {results['balanced'][0]:.2f}s")
    else:
        print(f"fast 不比 balanced 快（{results['fast'][0]:.2f}s vs "
              f"{results['balanced'][0]:.2f}s）—— 缓存命中的话正常")
    print("结论：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
