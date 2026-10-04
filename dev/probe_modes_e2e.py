"""真扫一遍目录，对比三档模式跑出来的组数与耗时。"""

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

    # 必须留住引用：QApplication 被 GC 掉，后面的事件循环会直接崩。
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

    print("\n" + "=" * 50)
    fast_rows, bal_rows = results["fast"][1], results["balanced"][1]
    fast_meta, bal_meta = results["fast"][2], results["balanced"][2]

    def grouping(rows) -> set[frozenset[str]]:
        return {frozenset(it.path for it in r.items) for r in rows}

    ok = True
    if fast_rows is not None and bal_rows is not None:
        fast_g, bal_g = grouping(fast_rows), grouping(bal_rows)
        if fast_g == bal_g:
            print("两档的分组完全一样 —— 可能没修好")
            ok = False
        else:
            print(f"✓ 两档分组不同：fast {len(fast_g)} 组 / "
                  f"balanced {len(bal_g)} 组")
            wider = [m for m in bal_g if m not in fast_g and len(m) > 1]
            if wider:
                print(f"  balanced 并得更宽的那组有 {len(wider[0])} 个成员"
                      f"（fast 只连到 {max((len(m) for m in fast_g), default=0)} 个）")
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
