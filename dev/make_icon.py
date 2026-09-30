"""从原图生成应用图标（`resources/icon.ico` + `resources/icon.png`）。

为什么要有这个脚本，而不是手工丢一个 ico 进仓库？
图标是可复现的派生产物：改一次配色、换一版 logo 就要重出一套。
把"怎么从原图得到多尺寸 ico"写死在代码里，下次换图只跑一条命令，
不用再回忆"当时用什么工具、勾了哪些尺寸"。

用法：
    python -m dev.make_icon                      # 用默认原图
    python -m dev.make_icon --src <图片路径>      # 换一张原图

产出：
    resources/icon.ico   多尺寸（256/128/64/48/32/24/16），给 exe 的 PE 资源 + 快捷方式
    resources/icon.png   256x256，给运行时的 setWindowIcon()

图标必须是正方形。Windows 的图标位图按正方形处理，
把宽幅图直接塞进去会被拉伸压扁。本脚本的做法是：
按 alpha 裁掉透明边 → 居中放进正方形画布（四周留白），
既不裁内容也不拉伸。留白而不是裁切，是因为这类 logo 的四角
（叶尖、飘带）一裁就残。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES = PROJECT_ROOT / "resources"

#: 默认原图。放在项目外（用户桌面），所以只在显式不带 --src 时兜底用它。
DEFAULT_SRC = Path(r"C:\Users\lightning\Desktop\ImgDedup.png")

#: ICO 里要包含的尺寸。16/32/48 是资源管理器各视图用的，
#: 256 是"大图标"视图，128/64/24 补齐中间档避免缩放发虚。
ICON_SIZES = (256, 128, 64, 48, 32, 24, 16)

#: 运行时用的大图边长
PNG_SIDE = 256

#: 正方形画布四周留白比例（相对长边）。0.04 在"不贴边"和"不浪费像素"之间。
PAD_RATIO = 0.04


def build(src: Path, *, out_dir: Path = RESOURCES) -> tuple[Path, Path]:
    """从 `src` 生成 ico + png，返回两个产物路径。"""
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

    # 正方形画布：边长取内容长边 + 两侧留白。内容短边方向自然居中留白。
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
    print(f"写出      : {png_path.relative_to(PROJECT_ROOT)} ({PNG_SIDE}x{PNG_SIDE})")

    # ICO：PIL 会把每一帧按各自尺寸存进去。
    # `sizes=` 和 `append_images` 要一起给：只给 sizes 而不给 frames，
    # PIL 会把同一张图重采样成各尺寸（也能用，但大图缩小的质量不如自己控）。
    frames = [canvas.resize((s, s), Image.LANCZOS) for s in ICON_SIZES]
    ico_path = out_dir / "icon.ico"
    frames[0].save(ico_path, format="ICO",
                   sizes=[(s, s) for s in ICON_SIZES],
                   append_images=frames[1:])
    print(f"写出      : {ico_path.relative_to(PROJECT_ROOT)} "
          f"({', '.join(str(s) for s in ICON_SIZES)})")

    # 回读确认真的写进去了：别只信 save() 没报错
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
