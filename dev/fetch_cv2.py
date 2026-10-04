"""准备外置组件 cv2，放到 `<exe 同级>/extra/cv2/`。

可以从 PyPI 下，也可以从本机已装的 cv2 拷过来。"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from imgdedup_core import cv2_fetch as F

DEFAULT_OUT = PROJECT_ROOT / "dist" / "ImgDedup" / "extra"


def source_from_installed() -> Path:
    try:
        import cv2
    except ImportError as e:
        raise SystemExit(
            "本机没有可用的 cv2，无法从这里拷贝。\n"
            "可以改用： python -m dev.fetch_cv2 --download\n"
            f"（底层报错：{e}）")
    p = Path(cv2.__file__).resolve().parent
    print(f"来源：本机已安装的 cv2 {getattr(cv2, '__version__', '?')}")
    print(f"      {p}")
    return p


def copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    total = 0
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if F.should_skip(rel.parts):
            continue
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        n += 1
        total += p.stat().st_size
    return n, total


def make_zip(folder: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(folder.parent))
    print(f"压缩包：{zip_path}  "
          f"({zip_path.stat().st_size / 1024 / 1024:.1f} MiB)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="准备外置组件 cv2，放到 <exe 同级>/extra/cv2/")
    ap.add_argument("--out", nargs="?", const="", default=None, type=str,
                    help=f"目标 extra 目录；不给 `--out` 则用 {DEFAULT_OUT}"
                         f"（注意 fetch_model 的默认值是开发目录）")
    ap.add_argument("--download", action="store_true",
                    help="从 PyPI 下载（本机没装 cv2 时用）")
    ap.add_argument("--zip", nargs="?", const="", default=None,
                    help="顺便打一个压缩包；不给路径则放在 dist/cv2-win64.zip")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else DEFAULT_OUT
    dst = out / "cv2"
    if (dst / "cv2.pyd").is_file():
        print(f"目标已有 cv2：{dst}\n（会覆盖同名文件；要重来就先把它删掉）")

    if args.download:
        def cb(_a, _b, msg):
            print(f"  {msg}", flush=True)

        print("从 PyPI 获取…")
        n, total = F.install(out, cb=cb)
    else:
        src = source_from_installed()
        if not (src / "cv2.pyd").exists():
            raise SystemExit(
                f"{src} 里没有 cv2.pyd —— 这不像一个 Windows 版的 cv2 包。\n"
                f"（Linux/macOS 上是 .so/.dylib，本脚本只处理 Windows）")
        n, total = copy_tree(src, dst)

    print(f"\n已放好：{dst}")
    print(f"  {n} 个文件，{total / 1024 / 1024:.1f} MiB")
    print(f"  关键文件：{(dst / 'cv2.pyd').stat().st_size / 1024 / 1024:.1f} MiB"
          f"  cv2.pyd")

    if args.zip is not None:
        zip_path = Path(args.zip) if args.zip else \
            (PROJECT_ROOT / "dist" / "cv2-win64.zip")
        make_zip(dst, zip_path)
        print("\n把这个压缩包发给用户，让他解压后把 cv2 文件夹放进 extra\\ 即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
