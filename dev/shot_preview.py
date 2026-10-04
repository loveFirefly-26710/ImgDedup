"""截图：主窗口整体。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath("."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from imgdedup_ui import theme
from imgdedup_ui.model import GroupRow, ItemRow
from imgdedup_ui.window import MainWindow

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("E:/1/test")
out = Path(".scratch/shots_preview")
out.mkdir(parents=True, exist_ok=True)

app = QApplication.instance() or QApplication([])
theme.apply_theme(app, "mint")

files = sorted(
    p for p in SRC.rglob("*")
    if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
)[:24]
if not files:
    print("没找到图片，退出")
    raise SystemExit(1)


def mk(path: Path) -> ItemRow:
    try:
        st = path.stat()
    except OSError:
        st = None
    size = st.st_size if st else 0
    return ItemRow(path=str(path).replace("\\", "/"), size=size,
                   width=0, height=0, fmt=path.suffix.lstrip(".").upper())


rows = []
for g in range(3):
    chunk = files[g * 8:(g + 1) * 8]
    if not chunk:
        break
    r = GroupRow(kind="exact", label="完全一致", items=[mk(p) for p in chunk],
                 keeps={0})
    r.auto_decide()
    rows.append(r)

win = MainWindow()
win.resize(1360, 880)
win.model.set_rows(rows)
win.show()
app.processEvents()
win._sync_view_width()
app.processEvents()


def settle(msec: int = 1200) -> None:
    loop = QEventLoop()
    QTimer.singleShot(msec, loop.quit)
    loop.exec()
    app.processEvents()


def shot(name: str, note: str = "") -> None:
    app.processEvents()
    win.grab().save(str(out / name))
    print(f"  {name}  {note}")


settle(800)
shot("01_empty_preview.png", "未选中 → 提示语")

win._set_selected(0, 2)
settle(2000)
shot("02_preview_shown.png", "选中第 3 张 → 预览大图")

win.sld_thumb.setValue(180)
win._on_thumb_size_changed(180)
settle(1500)
shot("03_thumb_180.png", "缩略图 180px")

win.sld_thumb.setValue(72)
win._on_thumb_size_changed(72)
settle(1500)
shot("04_thumb_72.png", "缩略图 72px（概览）")

win._set_selected(1, 5)
settle(2000)
shot("05_preview_switched.png", "切到第 2 组第 6 张 → 预览跟着换")

theme.apply_theme(app, "aurora")
win.cmb_theme.setCurrentIndex(win.cmb_theme.findData("aurora"))
settle(800)
shot("06_theme_aurora.png", "极光紫主题下的预览框")

print(f"\n预览框尺寸: {win.preview.width()}x{win.preview.height()}"
      f"（在右栏内，右栏宽 {win.right_split.width()}）")
print(f"右栏布局: 上=预览 / 下=信息+按钮，两格都不可折叠: "
      f"{not win.right_split.isCollapsible(0)} / "
      f"{not win.right_split.isCollapsible(1)}")
print(f"缩略图当前: {win.delegate.thumb_size}px, 每行 {win.delegate.per_line()} 张")

win.thumbs.shutdown()
win.preview_cache.shutdown()
