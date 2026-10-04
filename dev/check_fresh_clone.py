
"""模拟「全新克隆」：核对 .gitignore 没漏文件，再把清单拷到临时目录跑一遍测试。

仓库当前没有 tests/ 目录，所以最后那一步会空跑（discover 到 0 项）。
.gitignore 漏网检查与测试无关，仍然有效 —— 别把整个脚本当成没用的。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

PROJ = pathlib.Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = ("dist/", "build/", ".scratch/", "resources/models/")


def listed_files() -> list[str]:
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false",
         "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=str(PROJ), capture_output=True, text=True, check=True)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def main() -> int:
    files = listed_files()
    present = [f for f in files if (PROJ / f).is_file()]
    gone = sorted(set(files) - set(present))
    print(f"新克隆会有 {len(present)} 个文件")
    if gone:
        print(f"有 {len(gone)} 个已 staged 但盘上已删，跳过：{gone}")
        print("   （建议 `git rm --cached <路径>` 摘掉，别提交进去）")

    leak = [f for f in present if f.startswith(MUST_BE_IGNORED)]
    if leak:
        print("!! .gitignore 漏了这些，它们会被提交进仓库：")
        for f in leak[:10]:
            print("   -", f)
        return 2
    print("gitignore 没漏（dist / build / .scratch / resources/models 都不在清单里）")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="imgdedup_clone_"))
    try:
        for rel in present:
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJ / rel, dst)
        print(f"已拷到 {tmp}")

        env = dict(os.environ)
        env["IMGDEDUP_MODEL_DIR"] = str(tmp / "_empty_cache")
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["IMGDEDUP_TEST_LIBRARY"] = str(tmp / "_no_such_library")

        print("\n跑全量测试…")
        r = subprocess.run(
            [sys.executable, "-u", "-m", "unittest", "discover",
             "-s", "tests", "-p", "test_*.py", "-v"],
            cwd=str(tmp), env=env, timeout=1800)
        print(f"\n退出码: {r.returncode}"
              + ("（0 = 新克隆里也全过 ✅）" if r.returncode == 0
                 else "（非 0 = 新克隆里跑不过，别推 ❌）"))
        return r.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
