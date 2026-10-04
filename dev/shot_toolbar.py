"""截图：工具栏。"""

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
    for width in (1360, 1100):
        win.resize(width, 300)
        win.show()
        app.processEvents()
        for i in range(win.cmb_mode.count()):
            win.cmb_mode.setCurrentIndex(i)
            app.processEvents()
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
