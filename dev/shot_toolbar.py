"""离屏截图：工具栏在窄窗口下会不会被模式说明挤坏。

为什么单独做：工具栏是 `QHBoxLayout`，一行里塞了输入框(可伸缩) + 按钮 +
两个下拉框 + 说明标签 + 滑块 + 三个按钮。说明文字一长就可能把滑块挤没、
或者自己折行糊成一团：这类问题纯逻辑测试测不出来。

中文在离屏渲染下是方块，只判断「控件还在不在、有没有互相压住」，
不判断文案。

用法： python dev/shot_toolbar.py [输出目录]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from imgdedup_ui import theme
    from imgdedup_ui.window import MainWindow

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (PROJ / ".scratch" / "toolbar")
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, theme.DEFAULT_THEME)

    win = MainWindow()
    # 三档 × 两个宽度：1360 是设计宽度，1100 是用户把窗口拖窄后的样子
    for width in (1360, 1100):
        win.resize(width, 300)
        win.show()
        app.processEvents()
        for i in range(win.cmb_mode.count()):
            win.cmb_mode.setCurrentIndex(i)
            app.processEvents()
            # 直接抓工具栏本身
            tb = win.ed_root.parentWidget()
            pm = tb.grab()
            name = f"toolbar_{width}_mode{i}.png"
            pm.save(str(out / name))
            sld = win.sld
            print(f"[ok] {name}  slider_visible={sld.isVisible()} "
                  f"slider_w={sld.width()}  hint_w={win.lbl_mode.width()}")
    win.close()
    print(f"\n输出目录：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
