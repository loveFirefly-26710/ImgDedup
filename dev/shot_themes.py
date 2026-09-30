"""离屏截图：五套主题各来一张，用来人眼过一遍配色。

为什么必须做这一步：渐变背景 / 卡片投影 / 主按钮渐变全是自绘或
`qlineargradient`，纯逻辑测试只能断言"值对不对"，看不出"好不好看、
有没有糊成一团"。上一次多段进度条压字就是这样漏掉的。

本机离屏渲染中文会缺字（环境无 CJK 字体），所以只看
「色块 / 渐变方向 / 层级关系 / 投影是否可见」，不要据此判断文案。

用法： python dev/shot_themes.py [输出目录]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    from imgdedup_ui import theme
    from imgdedup_ui.model import (DELETE, KEEP, UNDECIDED,
                                   GroupRow, ItemRow)
    from imgdedup_ui.window import MainWindow

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (PROJ / ".scratch" / "themes")
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])

    def rows():
        made = []
        plan = [("exact", 6), ("perceptual", 9), ("semantic", 4)]
        for kind, n in plan:
            items = [ItemRow(path=f"X:/photos/{kind}_{i:03d}.jpg",
                             size=1_200_000 - i * 9000,
                             width=4032 - i * 4, height=3024 - i * 3,
                             decision=(KEEP if i == 0 else
                                       DELETE if i == 1 else UNDECIDED))
                     for i in range(n)]
            made.append(GroupRow(kind=kind, items=items,
                                 label=f"{kind} · {n} 张", oversized=False))
        return made

    win = MainWindow()
    win.resize(1360, 860)
    win.model.set_rows(rows())
    win.show()
    app.processEvents()
    win._sync_view_width()
    app.processEvents()

    n = 0
    for key in theme.THEMES:
        idx = win.cmb_theme.findData(key)
        if idx < 0:
            continue
        if win.cmb_theme.currentIndex() == idx:
            win._on_theme_changed(idx)   # 同一项 Qt 不发信号，显式来一次
        else:
            win.cmb_theme.setCurrentIndex(idx)
        app.processEvents()
        win._sync_view_width()
        app.processEvents()

        img = QImage(win.size(), QImage.Format_ARGB32)
        img.fill(0)
        win.render(img)
        p = out / f"{key}.png"
        img.save(str(p))
        print(f"[ok] {key:10s} -> {p}")
        n += 1

    theme.apply_theme(app, theme.DEFAULT_THEME)
    print(f"\n共 {n} 张，输出目录：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
