r"""产物级验收：冻结产物自检 + 外置组件 / AGPL 红线 + 体积报告。

## 为什么抽成脚本，而不是内联在 workflow 里

原来这两件事是 PowerShell 内联在 `ci.yml` 的打包 job 里的。抽出来的两条理由：

1. **发布流程必须跑同一套检查。** 推 tag 不触发 `ci.yml`（那个 workflow 只监听
   分支），所以 `release.yml` 得自己再跑一遍：否则一个从没走过打包冒烟的 commit
   打上标签就能直接发出去。推分支时 `ci.yml` 会跑同一批检查，但发布路径不依赖
   "另一个 workflow 大概跑过了"。两处各写一份 PowerShell，迟早会漂移成两套判据。
2. **PowerShell 的 `&` 不等 GUI 子系统进程。** 产物 exe 是 `console=False` 的
   GUI 程序：`& .\ImgDedup.exe --selftest` 实测 0.09 秒就返回、`$LASTEXITCODE`
   是 `$null`，于是 `$null -ne 0` 成立，报出"自检退出码 "（后面一片空白）——
   自检明明通过了却被判失败。`subprocess.run` 天然会等进程退出并返回真实退出码，
   没有这个坑。

## 检查项

| # | 检查 | 判据 |
|---|---|---|
| 1 | 产物 exe 在不在 | 不在直接 FAIL |
| 2 | `--selftest` | 退出码 0，且结果文件里 `ok == true` |
| 3 | 外置组件没被打回包 | `_internal/` 下没有 `models` / `cv2` / `cv2.pyd` |
| 4 | AGPL 红线 | `_internal/` 下没有 `pymupdf` / `fitz` / `_mupdf*` |
| 5 | 体积报告 | 打印 exe / `_internal` / `extra` 大小（不判失败） |

判据是**产物**而不是源码：`--selftest` 证明"这个包跑起来是对的"，
红线扫描证明"这个包没把 170 MB 的外置组件和 AGPL 组件卷进去"。
两者都过了，还差一步"这个包是不是本次的代码"——那条在
`dev/check_frozen.py`（读 exe 内嵌的 PYZ 逐项核对符号）。

用法：
    python dev/check_artifacts.py                      # 默认 dist/ImgDedup/ImgDedup.exe
    python dev/check_artifacts.py path/to/ImgDedup.exe
    python dev/check_artifacts.py <exe> --no-selftest   # 只做静态红线扫描

退出码 0 = 通过，1 = 有硬失败。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = PROJECT_ROOT / "dist" / "ImgDedup" / "ImgDedup.exe"

#: 外置组件与 AGPL 组件都不该出现在 `_internal/` 里。
#: 名字 → 它是什么（写进失败信息，免得看到 `fitz` 还要去查）。
FORBIDDEN_INTERNAL = {
    "models": "外置 CLIP 模型 85 MB，应放 <exe 同级>/extra/models",
    "cv2": "外置 cv2 86 MB，应放 <exe 同级>/extra/cv2",
    "cv2.pyd": "外置 cv2 86 MB，应放 <exe 同级>/extra/cv2",
    "pymupdf": "AGPL 组件",
    "fitz": "AGPL 组件，PyMuPDF 的包名",
}

#: 还有一类是 PyMuPDF 打包后散出来的文件名（`_mupdf.cp313-win_amd64.pyd` 等），
#: 按前缀兜一道。
FORBIDDEN_INTERNAL_PREFIX = ("_mupdf",)


def _mib(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MiB"


def _tree_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def forbidden_entries(root: Path) -> list[str]:
    """扫 `<onedir>/_internal`，返回违规项描述。空列表 = 干净。

    单独拆成函数是为了能直接测：给它一个造出来的假 `_internal` 目录，
    就能断言"塞回模型/AGPL 组件会被抓到"，不必真打一次包。
    """
    internal = root / "_internal"
    if not internal.is_dir():
        return [f"找不到 {internal} —— 产物不是 onedir 形态？"]
    bad: list[str] = []
    for name, what in FORBIDDEN_INTERNAL.items():
        if (internal / name).exists():
            bad.append(f"_internal/{name}（{what}）")
    for p in sorted(internal.iterdir()):
        if p.name.startswith(FORBIDDEN_INTERNAL_PREFIX):
            bad.append(f"_internal/{p.name}（AGPL 组件）")
    return bad


def run_selftest(exe: Path, timeout: int = 600) -> tuple[bool, list[str]]:
    """跑 `ImgDedup.exe --selftest`，返回 (是否通过, 自检日志行)。

    判据是**退出码 + `IMGDEDUP_SELFTEST_OUT` 指向的结果文件**，不是 stdout：
    窗口版 exe 没有控制台，`print` 出来的东西收不到（这也是项目一开始就
    选结果文件当判据的原因）。
    """
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "selftest.json"
        env = dict(os.environ)
        env["IMGDEDUP_SELFTEST_OUT"] = str(out)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                [str(exe), "--selftest"], env=env, timeout=timeout,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace")
        except subprocess.TimeoutExpired:
            return False, [f"自检超时（>{timeout}s）—— 多半是挂在某个导入上"]
        except OSError as e:
            return False, [f"自检起不来：{type(e).__name__}: {e}"]

        raw = proc.stdout or ""
        if proc.returncode != 0:
            return False, [f"自检退出码 {proc.returncode}", raw.strip()]

        if not out.is_file():
            # 退出码 0 但没写结果文件：说明自检根本没跑到写文件那一步
            # （比如 exe 其实是个空壳）。不能只看退出码。
            return False, ["自检退出码 0，但没有写出结果文件 —— "
                           "不能只看退出码", raw.strip()]
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return False, [f"结果文件读不出来：{type(e).__name__}: {e}"]

        lines = list(data.get("lines") or [])
        if not data.get("ok"):
            return False, lines or ["自检报告 ok=false"]
        return True, lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="冻结产物的自检与红线扫描")
    ap.add_argument("exe", nargs="?", default=str(DEFAULT_EXE),
                    help=f"产物 exe 路径（默认 {DEFAULT_EXE}）")
    ap.add_argument("--no-selftest", action="store_true",
                    help="跳过 --selftest，只做静态红线扫描与体积报告")
    ap.add_argument("--timeout", type=int, default=600,
                    help="自检超时秒数（默认 600）")
    args = ap.parse_args(argv)

    exe = Path(args.exe)
    root = exe.parent
    ok = True

    print("=" * 62)
    print("  产物级验收")
    print("=" * 62)
    print(f"  exe        : {exe}")

    if not exe.is_file():
        print("  !! 没有这个文件 —— 先打包（python -m dev.make_installer --build）")
        print("\n  结论：FAIL ❌")
        return 1

    # 1) 自检
    if args.no_selftest:
        print("  自检        : 跳过（--no-selftest）")
    else:
        good, lines = run_selftest(exe, timeout=args.timeout)
        print(f"  自检        : {'OK' if good else 'FAIL'}")
        for ln in lines:
            print(f"    {ln}")
        ok = ok and good

    # 2) 外置组件 / AGPL 红线
    bad = forbidden_entries(root)
    if bad:
        print("  红线扫描    : FAIL —— 产物里出现了不该有的东西")
        for b in bad:
            print(f"    !! {b}")
        ok = False
    else:
        print("  红线扫描    : OK（没有外置组件、也没有 AGPL 组件）")

    # 3) 体积报告（只报数，不判失败）
    print("  体积        :")
    print(f"    exe       : {_mib(exe.stat().st_size)}")
    internal = root / "_internal"
    if internal.is_dir():
        print(f"    _internal : {_mib(_tree_size(internal))}")
    extra = root / "extra"
    if extra.is_dir():
        has_cv2 = (extra / "cv2" / "cv2.pyd").is_file()
        print(f"    extra     : {'已放入 cv2' if has_cv2 else '待用户放入 cv2'}"
              f"（外置，不计入安装包）")

    print()
    print("  结论：" + ("PASS ✅" if ok else "FAIL ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
