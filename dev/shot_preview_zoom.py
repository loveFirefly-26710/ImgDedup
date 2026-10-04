"""截图：预览区的缩放与动图状态。"""

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

    h, w = 300, 400
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = np.linspace(30, 230, w, dtype=np.uint8)[None, :]
    arr[:, :, 1] = np.linspace(200, 60, h, dtype=np.uint8)[:, None]
    arr[:, :, 2] = 140
    arr[::20, :, :] = 255
    arr[:, ::20, :] = 255
    from imgdedup_ui.widgets import _pixmap_from_rgb
    pane._pixmap = _pixmap_from_rgb(arr)

    pane.reset_zoom()
    pane.repaint()
    pane.grab().save(str(out / "01_fit.png"))
    print("[ok] 01_fit.png")

    pane.zoom_by(2.5, QPointF(120, 140))
    pane.repaint()
    pane.grab().save(str(out / "02_zoom250.png"))
    print(f"[ok] 02_zoom250.png  zoom={pane._zoom:.2f} "
          f"pan=({pane._pan.x():.1f},{pane._pan.y():.1f})")

    for _ in range(30):
        pane.zoom_by(1.15)
    pane.repaint()
    pane.grab().save(str(out / "03_zoom_max.png"))
    print(f"[ok] 03_zoom_max.png  zoom={pane._zoom:.2f}")

    for _ in range(60):
        pane.zoom_by(1 / 1.15)
    pane.repaint()
    pane.grab().save(str(out / "04_zoom_min.png"))
    print(f"[ok] 04_zoom_min.png  zoom={pane._zoom:.2f}")

    class _FakeAnim:
        frames = [arr.copy() for _ in range(12)]
        delays = [60] * 12
        note = "帧数过多，只播前 12 帧（原 480 帧）"
        truncated = True

    pane.reset_zoom()
    pane._anim_token = 1
    pane._on_anim_ready(1, _FakeAnim())
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
