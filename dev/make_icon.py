"""生成应用图标（ico + png）。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES = PROJECT_ROOT / "resources"

DEFAULT_SRC = Path(r"C:\Users\lightning\Desktop\ImgDedup.png")

ICON_SIZES = (256, 128, 64, 48, 32, 24, 16)

PNG_SIDE = 256

PAD_RATIO = 0.04


def _shown(p: Path) -> str:
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def build(src: Path, *, out_dir: Path = RESOURCES) -> tuple[Path, Path]:
    from PIL import Image

    if not src.is_file():
        raise SystemExit(f"找不到原图：{src}")

    im = Image.open(src).convert("RGBA")
    print(f"原图      : {src}")
    print(f"  尺寸    : {im.size[0]}x{im.size[1]}  模式 RGBA")

    bbox = im.getbbox()
    if bbox is None:
        raise SystemExit("原图整幅全透明，没有可用的图像内容")
    art = im.crop(bbox)
    print(f"  裁透明边: {art.size[0]}x{art.size[1]}  "
          f"（宽高比 {art.size[0] / art.size[1]:.2f}）")

    side = max(art.size)
    pad = int(side * PAD_RATIO)
    canvas_side = side + pad * 2
    canvas = Image.new("RGBA", (canvas_side, canvas_side), (0, 0, 0, 0))
    canvas.paste(art, ((canvas_side - art.size[0]) // 2,
                       (canvas_side - art.size[1]) // 2), art)
    print(f"  方形画布: {canvas_side}x{canvas_side}（留白 {pad}px）")

    out_dir.mkdir(parents=True, exist_ok=True)

    png_path = out_dir / "icon.png"
    canvas.resize((PNG_SIDE, PNG_SIDE), Image.LANCZOS).save(png_path)
    print(f"写出      : {_shown(png_path)} ({PNG_SIDE}x{PNG_SIDE})")

    frames = [canvas.resize((s, s), Image.LANCZOS) for s in ICON_SIZES]
    ico_path = out_dir / "icon.ico"
    frames[0].save(ico_path, format="ICO",
                   sizes=[(s, s) for s in ICON_SIZES],
                   append_images=frames[1:])
    print(f"写出      : {_shown(ico_path)} "
          f"({', '.join(str(s) for s in ICON_SIZES)})")

    with Image.open(ico_path) as chk:
        got = sorted(s for s, _ in chk.info.get("sizes", []))
    want = sorted(ICON_SIZES)
    if got != want:
        raise SystemExit(f"ICO 尺寸不符：期望 {want}，实际 {got}")
    print(f"校验      : ICO 含 {len(got)} 个尺寸，全部就位")
    return ico_path, png_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成应用图标（ico + png）")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC,
                    help=f"原图路径（默认 {DEFAULT_SRC}）")
    ap.add_argument("--out", type=Path, default=RESOURCES,
                    help="输出目录（默认 resources/）")
    args = ap.parse_args(argv)

    print("=" * 62)
    print("  生成应用图标")
    print("=" * 62)
    build(args.src, out_dir=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
