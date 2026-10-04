"""在冻结产物里跑探针脚本的引导程序。"""

from __future__ import annotations

import multiprocessing
import sys


def _force_utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _force_utf8()
    print("=" * 66)
    print("  ImgDedup 冻结产物自检")
    print("=" * 66)
    print(f"  frozen    : {getattr(sys, 'frozen', False)}")
    print(f"  _MEIPASS  : {getattr(sys, '_MEIPASS', None)}")
    print(f"  executable: {sys.executable}")
    print(f"  cwd       : {__import__('os').getcwd()}")

    from imgdedup_core import embed as EMB

    print(f"\n  推荐位置    : {EMB.model_extra_dir()}")
    print(f"  bundled_root: {EMB.bundled_root()}")
    print(f"  model_dir   : {EMB.model_dir()}")
    print(f"  is_ready    : {EMB.is_ready()}")

    if not EMB.is_ready():
        print("\n  模型未安装（外置组件，可缺）—— 深度模式会自动跳过语义层。")
        print("  想验证加载：把模型放进上面的「推荐位置」后重跑。")
        return 0

    print(f"  模型来源    : "
          f"{'随程序目录' if EMB.is_bundled() else '本地缓存'}")

    from imgdedup_core import opencv as OPENCV

    if not OPENCV.is_available():
        print("\n  跳过推理：cv2 未安装（推理链路需要它）")
        print(f"  应放到 {OPENCV.cv2_dir()}")
        return 0

    print("\n  加载并推理（应当零联网）：")
    enc = EMB.ClipEncoder(verbose=True)
    if not enc.load():
        print("  !! 模型在位但加载失败")
        return 2

    import numpy as np

    r = EMB.encode_all(enc, [np.zeros((224, 224, 3), np.uint8)], batch=1)
    print(f"  encode: ok={r.ok} dim={r.dim} {r.note}")
    if not r.ok:
        return 3

    print("\n  自检通过 ✅")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
