"""冻结产物自检入口（打包用）。

验证两件事：
  1. 冻结环境下模型路径能解析到（`extra/models` 或其它候选位置）；
  2. 模型在位时能真的加载并推理，且不需要联网。

模型现在是外置组件（和 cv2 一样不打进包），所以"没放模型"
是发布形态的正常状态，不是失败：此时本探针只报告一条说明。
真正的"包坏了"要由别的判据去抓。
"""

from __future__ import annotations

import multiprocessing
import sys


def _force_utf8() -> None:
    """把 stdout/stderr 重绑成 UTF-8。

    冻结后控制台默认是 GBK（本机 cp936），打印 ✅ 这类字符会直接抛
    UnicodeEncodeError 并让自检以错误码退出：实测踩过。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
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
