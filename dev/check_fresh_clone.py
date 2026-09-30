"""模拟全新克隆：按 git 的口径拷一份出去，在临时目录里跑全量测试。

## 为什么需要

"本机测试全绿"和"CI 会绿"是两件事。 本机有模型、有 `dist/`、
有用户缓存，一整套本地环境会把"只在干净环境才暴露"的问题盖住。

实测抓到过一次（代价很大）：

    `_guard_deep_mode` 原本挂在 `currentIndexChanged` 上，而这个信号
    程序化 `setCurrentIndex` 也会触发 → 测试把档位设成「深度」时弹出
    模态对话框，没人点它 → 整个测试套件挂死十几分钟。
    本机因为模型在位、`is_ready()` 为真，从来不触发。
    CI 上就是挂到 job 超时。

## 为什么不"把 resources/models 改名藏起来"

那要移动用户的文件：进程被强杀（SIGTERM 不走 `finally`）就留下残局 ——
这个坑真踩过两次。本脚本改成拷一份到临时目录，不动原仓库任何东西。

## 用法

    python dev/check_fresh_clone.py

退出码非 0 = 新克隆里跑不过，别推。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

PROJ = pathlib.Path(__file__).resolve().parent.parent

#: 这些大目录绝不该出现在"新克隆清单"里（出现说明 .gitignore 漏了）
MUST_BE_IGNORED = ("dist/", "build/", ".scratch/", "resources/models/")


def listed_files() -> list[str]:
    """新克隆里会有的文件（已跟踪的 + 未忽略的未跟踪文件）。

    必须加 `-c core.quotepath=false`：否则 git 会把非 ASCII 文件名
    转义成 `"\\345\\220\\257..."` 这种八进制串，拿去当路径必然
    `FileNotFoundError`（第一次就栽在这里）。
    """
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false",
         "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=str(PROJ), capture_output=True, text=True, check=True)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def main() -> int:
    files = listed_files()
    present = [f for f in files if (PROJ / f).is_file()]
    # `--cached` 会列出"已 staged 但工作区已删"的文件（比如残留的备份），
    # 它们不在盘上，拷的时候会炸：跳过并点名。
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
        # 把模型缓存也指到空目录，尽量贴近 CI 的干净环境
        env["IMGDEDUP_MODEL_DIR"] = str(tmp / "_empty_cache")
        env["QT_QPA_PLATFORM"] = "offscreen"
        # 还要把"真实图库"也指到不存在的地方。
        # 本机有那个目录，不指走的话依赖它的测试在这里照样能跑。
        # 这条门禁就模拟不出 CI。实测踩过：一条
        # `assertTrue(self.entries, "拿不到真实图片")` 在真 CI 上会让
        # 7 项里崩 5 项，而当时这条门禁报的是绿。
        env["IMGDEDUP_TEST_LIBRARY"] = str(tmp / "_no_such_library")

        print("\n跑全量测试…")
        # 流式输出（不 capture）：一旦有测试卡住，日志会停在它名字后面，
        # 一眼定位。capture 的话卡住时什么都看不到，白等十几分钟。
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
