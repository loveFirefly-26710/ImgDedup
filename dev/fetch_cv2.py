"""把外置组件 cv2 准备好，放到免安装版（或指定目录）的 `extra/` 下。

## 背景

cv2 的 `cv2.pyd` 一个文件就 86 MB，占整个程序体积的 21.7%。
所以它不随包分发，改成"用户单独下载后放进 `extra/cv2/`"。

这个脚本是给打包的人用的：把 cv2 准备好、并可选打成一个压缩包发出去。

真正的下载/解压/筛选逻辑在 `imgdedup_core/cv2_fetch.py` ——
和程序里那个「自动下载」按钮共用同一份。
在这里另写一套的话，"自动下载"和"手动准备"迟早会选出不同的文件。

## 用法

    # 1) 从本机已装的 Python 拷（最省事，版本必然和开发环境一致）
    python -m dev.fetch_cv2

    # 2) 顺便打一个可分发的压缩包（发给最终用户的就是它）
    python -m dev.fetch_cv2 --zip

    # 3) 指定落到别的地方（比如某个已装好的 ImgDedup 目录）
    python -m dev.fetch_cv2 --out "D:/Programs/ImgDedup/extra"

    # 4) 从 PyPI 下（本机没装 cv2 时用；程序里的"自动下载"走的就是这条）
    python -m dev.fetch_cv2 --download --zip

## 最终用户拿到压缩包之后

解压，把里面的 `cv2` 文件夹整个放到 `ImgDedup.exe` 同级的 `extra\\` 下面，
即 `extra\\cv2\\cv2.pyd` 这个文件能被看到。重开程序即可。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from imgdedup_core import cv2_fetch as F  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "dist" / "ImgDedup" / "extra"


def _skip(name: str) -> bool:
    """这个文件名/目录名该不该跳过。委托给 core，保证两边一致。"""
    return F.should_skip((name,))


def source_from_installed() -> Path:
    """从当前 Python 环境里找到 cv2 包目录。"""
    try:
        import cv2  # noqa: PLC0415
    except ImportError as e:  # noqa: BLE001
        raise SystemExit(
            "本机没有可用的 cv2，无法从这里拷贝。\n"
            "可以改用： python -m dev.fetch_cv2 --download\n"
            f"（底层报错：{e}）")
    p = Path(cv2.__file__).resolve().parent
    print(f"来源：本机已安装的 cv2 {getattr(cv2, '__version__', '?')}")
    print(f"      {p}")
    return p


def copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    """把 cv2 包目录拷过去（跳过 `SKIP_*`）。返回 (文件数, 字节数)。"""
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
    """把 cv2 目录打成压缩包（顶层就是 `cv2/`，解压即用）。"""
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
    # `nargs="?", const=""` 让 `--out` 可以不带值（照文档跑却报
    # `expected one argument` 很劝退）。
    #
    # 但默认值和 `fetch_model` 不一样（实测确认，别以为两个脚本对称）：
    #   本脚本不给 `--out` → 落产物目录 dist/ImgDedup/extra/cv2/
    #   fetch_model 不给 `--out` → 落开发目录 resources/models/
    # 发布流程依赖这个语义，所以不改默认值，只写明。
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
