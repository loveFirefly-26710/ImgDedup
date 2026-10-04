"""冻结产物的自检与红线扫描。

跑 exe 自检，并扫 `_internal` 里有没有被打进来的外置组件或 AGPL 依赖。
判据全在产物里、不看源码，所以能抓到「spec 改坏了但源码测试全绿」。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = PROJECT_ROOT / "dist" / "ImgDedup" / "ImgDedup.exe"

FORBIDDEN_INTERNAL = {
    "models": "外置 CLIP 模型 85 MB，应放 <exe 同级>/extra/models",
    "cv2": "外置 cv2 86 MB，应放 <exe 同级>/extra/cv2",
    "cv2.pyd": "外置 cv2 86 MB，应放 <exe 同级>/extra/cv2",
    "pymupdf": "AGPL 组件",
    "fitz": "AGPL 组件，PyMuPDF 的包名",
}

FORBIDDEN_INTERNAL_PREFIX = ("_mupdf",)


def _mib(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MiB"


def _tree_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def forbidden_entries(root: Path) -> list[str]:
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
            return False, ["自检退出码 0，但没有写出结果文件 —— "
                           "不能只看退出码", raw.strip()]
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except Exception as e:
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

    if args.no_selftest:
        print("  自检        : 跳过（--no-selftest）")
    else:
        good, lines = run_selftest(exe, timeout=args.timeout)
        print(f"  自检        : {'OK' if good else 'FAIL'}")
        for ln in lines:
            print(f"    {ln}")
        ok = ok and good

    bad = forbidden_entries(root)
    if bad:
        print("  红线扫描    : FAIL —— 产物里出现了不该有的东西")
        for b in bad:
            print(f"    !! {b}")
        ok = False
    else:
        print("  红线扫描    : OK（没有外置组件、也没有 AGPL 组件）")

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
