"""获取外置 CLIP 模型，放到 `<exe 同级>/extra/models/`。"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from imgdedup_core import embed as EMB

DEV_ROOT = PROJECT_ROOT / "resources" / "models"
DEFAULT_EXTRA = PROJECT_ROOT / "dist" / "ImgDedup" / "extra"


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
    ap = argparse.ArgumentParser(description="获取外置 CLIP 模型")
    ap.add_argument("--force", action="store_true",
                    help="忽略已存在的文件，全部重下")
    ap.add_argument("--fp32", action="store_true",
                    help="下载全精度 vision encoder（335 MB），"
                         "默认用 int8 量化版（85 MB）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out", nargs="?", const="", default=None, type=str,
                    help=f"铺到 <该目录>/models/ 下；不给 `--out` 则用开发目录"
                         f" {DEV_ROOT}（给了 `--out` 但不带路径则用 {DEFAULT_EXTRA}）")
    ap.add_argument("--zip", nargs="?", const="", default=None,
                    help="顺便打一个压缩包；不给路径则放 "
                         "dist/models-clip-vit-base-patch32.zip")
    args = ap.parse_args(argv)

    files = EMB.MODEL_FILES_FP32 if args.fp32 else EMB.MODEL_FILES

    if args.out is None:
        root = DEV_ROOT
    else:
        extra = Path(args.out) if args.out else DEFAULT_EXTRA
        root = extra / EMB.BUNDLE_SUBDIR

    dest = root / EMB.repo_slug()
    print(f"目标目录: {dest}")
    print(f"模型变体: {'fp32' if args.fp32 else 'int8 量化'}  ({EMB.MODEL_REPO})")

    missing = [(r, l) for r, l in files.items()
               if args.force or not (dest / l).exists()]
    if not missing:
        print("已齐全，无需下载。")
    else:
        print(f"缺失 {len(missing)} 个文件：")
        local_src = DEV_ROOT / EMB.repo_slug()
        for remote, local in missing:
            src = local_src / local
            if src.exists() and src.resolve() != (dest / local).resolve():
                (dest / local).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / local)
                print(f"  拷贝 {local}  <- {src}")
                continue
            for mirror in EMB.MIRRORS:
                url = f"{mirror}/{EMB.MODEL_REPO}/resolve/main/{remote}"
                try:
                    if not args.quiet:
                        print(f"  <- {mirror}  {remote}")
                    EMB._download_file(url, dest / local, quiet=args.quiet)
                    break
                except Exception as e:
                    if not args.quiet:
                        print(f"     失败: {type(e).__name__} {str(e)[:70]}")
            else:
                print(f"  !! 所有镜像都失败: {remote}")
                return 1

    print("\n校验：")
    ok = True
    for name in files.values():
        p = dest / name
        if p.exists():
            print(f"  {name:<28} {p.stat().st_size / 1024**2:8.2f} MB")
        else:
            print(f"  {name:<28} 缺失")
            ok = False

    total = sum((dest / n).stat().st_size
                for n in files.values() if (dest / n).exists())
    print(f"  合计 {total / 1024**2:.1f} MB")

    if args.zip is not None:
        zip_path = Path(args.zip) if args.zip else \
            (PROJECT_ROOT / "dist" / f"models-{EMB.repo_slug()}.zip")
        make_zip(root, zip_path)
        print("\n把这个压缩包发给用户，让他解压后把 models 文件夹放进 extra\\ 即可。")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
