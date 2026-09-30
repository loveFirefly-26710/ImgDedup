"""离屏截图：预览框的缩放与动图浮标长什么样。

为什么单独做：浮标位置、缩放后图有没有糊/有没有溢出边框，纯逻辑测试
断言不了。上一次预览框从中间挪到侧栏，就是靠截图发现"最小尺寸撑破布局"的。

中文在离屏渲染下是方块：只看「图的位置 / 浮标是否压在图上 /
有没有越界」，不判断文案。

用法： python dev/shot_preview_zoom.py [输出目录]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    import numpy as np
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    from imgdedup_ui import theme
    from imgdedup_ui.model import ThumbCache
    from imgdedup_ui.widgets import PreviewPane

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (PROJ / ".scratch" / "preview")
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, theme.DEFAULT_THEME)

    cache = ThumbCache(size=512, capacity=4)
    pane = PreviewPane(cache)
    pane.resize(340, 420)

    # 造一张有结构的"图"：渐变 + 网格，方便看缩放对齐
    h, w = 300, 400
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = np.linspace(30, 230, w, dtype=np.uint8)[None, :]
    arr[:, :, 1] = np.linspace(200, 60, h, dtype=np.uint8)[:, None]
    arr[:, :, 2] = 140
    arr[::20, :, :] = 255
    arr[:, ::20, :] = 255
    from imgdedup_ui.widgets import _pixmap_from_rgb
    pane._pixmap = _pixmap_from_rgb(arr)

    # 1) 原样
    pane.reset_zoom()
    pane.repaint()
    pane.grab().save(str(out / "01_fit.png"))
    print("[ok] 01_fit.png")

    # 2) 放大 2 倍（以光标为锚点）
    pane.zoom_by(2.5, QPointF(120, 140))
    pane.repaint()
    pane.grab().save(str(out / "02_zoom250.png"))
    print(f"[ok] 02_zoom250.png  zoom={pane._zoom:.2f} "
          f"pan=({pane._pan.x():.1f},{pane._pan.y():.1f})")

    # 3) 放大到顶
    for _ in range(30):
        pane.zoom_by(1.15)
    pane.repaint()
    pane.grab().save(str(out / "03_zoom_max.png"))
    print(f"[ok] 03_zoom_max.png  zoom={pane._zoom:.2f}")

    # 4) 缩小到很小时的样子（确认不会缩成 0）
    for _ in range(60):
        pane.zoom_by(1 / 1.15)
    pane.repaint()
    pane.grab().save(str(out / "04_zoom_min.png"))
    print(f"[ok] 04_zoom_min.png  zoom={pane._zoom:.2f}")

    # 5) 动图浮标
    class _FakeAnim:
        frames = [arr.copy() for _ in range(12)]
        delays = [60] * 12
        note = "帧数过多，只播前 12 帧（原 480 帧）"
        truncated = True

    pane.reset_zoom()
    pane._anim_token = 1
    pane._on_anim_ready(1, _FakeAnim())
    pane._paint_badges_direct = True
    pane.repaint()
    pane.grab().save(str(out / "05_anim_badge.png"))
    print(f"[ok] 05_anim_badge.png  frames={len(pane._anim_frames)} "
          f"timer={pane._timer.isActive()}")
    pane._stop_anim()

    cache.shutdown()
    print(f"\n输出目录：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
