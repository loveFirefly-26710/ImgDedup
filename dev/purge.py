"""清理本项目产生的开发/测试垃圾（真删，不进回收站）。

这是路径 B 的入口。清理对象范围见 imgdedup_core/scratch.py 的模块文档。
默认会同时校验受保护的图库目录在整个过程中完全未被改动。

用法：
    python -m dev.purge --dry-run     # 先看会删什么
    python -m dev.purge               # 真删
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgdedup_core import scratch as SC  # noqa: E402

#: 受保护的图库：purge 的护栏保证它不可能被删，这里再独立校验一次
GUARDED_LIBRARY = r"E:\1\test"


def snapshot(path: str) -> tuple[int, int]:
    n = total = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            n += 1
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return n, total


def show_scratch() -> None:
    root = SC.scratch_root()
    entries = sorted(root.iterdir())
    if not entries:
        print("  (空)")
        return
    for e in entries:
        if e.is_dir():
            n, size = SC._count_only(e)
            print(f"  {e.name:<24} <目录>  {n:>6} 文件  {SC.human(size):>10}")
        else:
            print(f"  {e.name:<24}         {1:>6} 文件  {SC.human(e.stat().st_size):>10}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清理开发/测试垃圾（真删）")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不删除")
    ap.add_argument("--keep-scratch", action="store_true", help="保留 .scratch，只清构建缓存")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("  路径 B · 开发测试垃圾真删" + ("（预演，不实际删除）" if args.dry_run else ""))
    print("=" * 74)

    before = snapshot(GUARDED_LIBRARY)
    print(f"\n受保护图库快照（清理前）: {GUARDED_LIBRARY}")
    print(f"  {before[0]:,} 个文件 / {SC.human(before[1])}")

    print("\n--- .scratch/ 内容 ---")
    show_scratch()

    free_before = SC.shutil.disk_usage(str(SC.PROJECT_ROOT)).free

    results: list[dict] = []
    if not args.keep_scratch:
        results += SC.purge_scratch(dry_run=args.dry_run)
    results += SC.purge_build_junk(dry_run=args.dry_run)

    print("\n--- 清理结果 ---")
    SC.report(results, "purge")

    # 兜底：万一某次删除仍被重定向进回收站，这里按前缀清掉（含历史遗留）。
    # 只匹配 .scratch 之下的原始路径，绝不动回收站里别的东西。
    if not args.dry_run:
        roots = [SC.scratch_root()] + [SC.PROJECT_ROOT / n for n in SC.BUILD_JUNK_DIRS]
        swept = SC.sweep_recycle_bin(roots)
        print(f"  回收站兜底清扫: 移除 {swept} 个 $I/$R 条目"
              + ("（无残留）" if swept == 0 else ""))

    print("\n--- .scratch/ 内容（清理后）---")
    show_scratch()

    free_after = SC.shutil.disk_usage(str(SC.PROJECT_ROOT)).free
    if not args.dry_run:
        delta = free_after - free_before
        print(f"\n可用空间变化: {SC.human(delta)}"
              f"  （{'确已释放' if delta > 0 else '无明显变化——文件可能很小或被缓存'}）")

    after = snapshot(GUARDED_LIBRARY)
    print(f"\n受保护图库快照（清理后）: {after[0]:,} 个文件 / {SC.human(after[1])}")
    if before == after:
        print("  ✅ 图库未被改动")
    else:
        print(f"  ❌ 图库发生变化！before={before} after={after}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
