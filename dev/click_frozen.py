"""界面交互冒烟：离屏投真实鼠标事件（点选、右键、缩放、切图）。

由 `python -m imgdedup_ui --interact-test` 调用。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAIL: list[str] = []
LINES: list[str] = []


def _log(msg: str) -> None:
    LINES.append(msg)


def check(label: str, cond: bool, extra: str = "") -> None:
    _log(f"  {'OK ' if cond else 'FAIL'}  {label}"
         f"{(' — ' + extra) if extra else ''}")
    if not cond:
        FAIL.append(label)


def main() -> int:
    from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from imgdedup_ui.model import GroupRow, ItemRow
    from imgdedup_ui.window import ALL_KINDS, KIND_LABEL, MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1360, 860)

    rows = []
    for kind, n in [("exact", 6), ("perceptual", 9), ("cluster", 26)]:
        items = [ItemRow(path=f"X:/photos/{kind}_{i}.jpg", size=1_000_000,
                         width=4032, height=3024, fmt="JPEG") for i in range(n)]
        r = GroupRow(kind=kind, label="相似度 96%", items=items, keeps={0},
                     oversized=(kind == "cluster"))
        r.auto_decide()
        rows.append(r)
    win.model.set_rows(rows)
    win.show()
    app.processEvents()

    vp = win.view.viewport()
    d = win.delegate

    def vp_point(vrow: int, pos: int) -> QPoint:
        vr = win.view.visualRect(win.view.model().index(vrow, 0))
        card = QRect(0, 0, vr.width(), vr.height())
        return d._item_rect(card, pos).center() + vr.topLeft()

    def click(pt: QPoint, button=Qt.LeftButton) -> None:
        for et in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            app.sendEvent(vp, QMouseEvent(et, pt, vp.mapToGlobal(pt),
                                          button, button, Qt.NoModifier))
        app.processEvents()

    def shift_click(pt: QPoint) -> None:
        for et in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            app.sendEvent(vp, QMouseEvent(et, pt, vp.mapToGlobal(pt),
                                          Qt.LeftButton, Qt.LeftButton,
                                          Qt.ShiftModifier))
        app.processEvents()

    def key_ev(key, *, shift: bool = False, ctrl: bool = False):
        from PySide6.QtGui import QKeyEvent

        mod = Qt.NoModifier
        if shift:
            mod |= Qt.ShiftModifier
        if ctrl:
            mod |= Qt.ControlModifier
        return QKeyEvent(QEvent.KeyPress, key, mod)

    _log("=" * 58)
    _log(f"  ImgDedup 真实点击验证  (frozen={getattr(sys, 'frozen', False)})")
    _log("=" * 58)

    check("「全部」项有独立哨兵值", ALL_KINDS != "__sentinel__")
    check("窗口标题为 ImgDedup", win.windowTitle() == "ImgDedup",
          repr(win.windowTitle()))
    item0 = win.filter_list.item(0)
    check("「全部」项 UserRole 不是 None",
          item0 is not None and item0.data(Qt.UserRole) is not None,
          repr(item0.data(Qt.UserRole) if item0 else None))
    win._select_only(ALL_KINDS)
    app.processEvents()
    check("选「全部」→ 3 组都在", win.model.visible_count() == 3,
          f"got {win.model.visible_count()}")
    check("选「全部」→ 不过滤", win._active_kinds() is None)

    listed = [win.filter_list.item(i).data(Qt.UserRole)
              for i in range(1, win.filter_list.count())]
    check("筛选面板把四类都列出来（缺的也占一行）", listed == list(KIND_LABEL),
          repr(listed))

    kind_keys = [win.filter_list.item(i).data(Qt.UserRole)
                 for i in range(1, win.filter_list.count())
                 if win.filter_list.item(i).flags() & Qt.ItemIsEnabled]
    if kind_keys:
        win._select_only(kind_keys[0])
        app.processEvents()
        check("选某个分类 → 只筛该类型",
              win._active_kinds() == {kind_keys[0]},
              repr(win._active_kinds()))
        sel_n = sum(1 for i in range(win.filter_list.count())
                    if win.filter_list.item(i).isSelected())
        check("单选：选中项恰好 1 个", sel_n == 1, f"got {sel_n}")

        win.filter_list.setCurrentItem(win.filter_list.item(0))
        app.processEvents()
        check("点回「全部」→ 恢复 3 组", win.model.visible_count() == 3,
              f"got {win.model.visible_count()}")

    click(vp_point(1, 3))
    check("左键点缩略图 → 选中", (d.sel_vrow, d.sel_pos) == (1, 3),
          f"got {d.sel_vrow},{d.sel_pos}")
    click(vp_point(1, 3))
    check("再点一次 → 取消", (d.sel_vrow, d.sel_pos) == (-1, -1))

    click(vp_point(1, 1))
    check("单选 1 张", len(d.sel_set) == 1, f"got {len(d.sel_set)}")
    shift_click(vp_point(1, 5))
    check("Shift+左键 → 连选 5 张", len(d.sel_set) == 5,
          f"got {sorted(p for _v, p in d.sel_set)}")
    check("连选范围正确",
          {p for _v, p in d.sel_set} == {1, 2, 3, 4, 5},
          f"got {sorted(p for _v, p in d.sel_set)}")
    check("主选中项是 Shift 点的那张", d.anchor == (1, 5), f"got {d.anchor}")

    check("选中后预览挂上了路径", win.preview._path is not None,
          repr(win.preview._path))
    cur_row, cur_pos = win._current_pair()
    if cur_row is not None:
        check("预览的正是主选中那张",
              win.preview._path == cur_row.items[cur_pos].path,
              f"preview={win.preview._path} sel={cur_row.items[cur_pos].path}")
    check("预览缓存与网格缓存是两套",
          win.preview.cache is not win.thumbs)
    check("预览解码尺寸大于网格",
          win.preview_cache.size > win.thumbs.size,
          f"{win.preview_cache.size} vs {win.thumbs.size}")

    win._clear_selected()
    check("Esc/清空 → 无选中", not d.sel_set)
    check("清空选中 → 预览也清空", win.preview._path is None,
          repr(win.preview._path))

    check("右侧栏是上下分栏（上预览 / 下信息）", win.right_split.count() == 2)
    check("预览在右栏的上格",
          win.right_split.widget(0).isAncestorOf(win.preview))
    check("预览框不可被拖到消失", not win.right_split.isCollapsible(0))
    check("信息区不可被拖到消失", not win.right_split.isCollapsible(1))
    check("中间栏不再套 splitter（预览已搬走）",
          not hasattr(win, "center_split"))
    base_lines = d.per_line()
    win.sld_thumb.setValue(200)
    win._on_thumb_size_changed(200)
    app.processEvents()
    check("缩略图放大 → 每行张数变少", d.per_line() < base_lines,
          f"{base_lines} -> {d.per_line()}")
    check("滑块值传导到了 delegate", d.thumb_size == 200, f"got {d.thumb_size}")
    win.sld_thumb.setValue(72)
    win._on_thumb_size_changed(72)
    app.processEvents()
    check("缩略图缩小 → 每行张数变多", d.per_line() > 1, f"got {d.per_line()}")
    win.sld_thumb.setValue(112)
    win._on_thumb_size_changed(112)
    app.processEvents()

    win.view.setFocus(Qt.MouseFocusReason)
    app.processEvents()
    win._handle_selection_key(key_ev(Qt.Key_Right))
    check("无选中时按方向键 → 补上第一张",
          d.sel_set == {(1, 0)}, f"got {d.sel_set}")
    win._handle_selection_key(key_ev(Qt.Key_Right))
    win._handle_selection_key(key_ev(Qt.Key_Right))
    check("方向键连按 → 逐张右移", d.sel_set == {(1, 2)}, f"got {d.sel_set}")
    win._handle_selection_key(key_ev(Qt.Key_Left))
    check("方向键左移", d.sel_set == {(1, 1)}, f"got {d.sel_set}")
    win._handle_selection_key(key_ev(Qt.Key_Right, shift=True))
    check("Shift+方向键 → 连选", d.sel_set == {(1, 1), (1, 2)}, f"got {d.sel_set}")

    row1_n = win.model.row_at(1).count
    win._set_selected(1, row1_n - 1)
    win._handle_selection_key(key_ev(Qt.Key_Right))
    check("行尾再右移 → 换到下一组", d.sel_set == {(2, 0)}, f"got {d.sel_set}")

    win._clear_selected()
    win._set_selected(1, 1)
    win._handle_selection_key(key_ev(Qt.Key_A, ctrl=True))
    check("Ctrl+A → 全选本组",
          len(d.sel_set) == win.model.row_at(1).count, f"got {len(d.sel_set)}")
    win._clear_selected()

    win.ed_root.setFocus()
    app.processEvents()
    check("焦点在输入框 → 方向键放行",
          not win._handle_selection_key(key_ev(Qt.Key_Right)),
          "网格把输入框的方向键吞了")

    win._set_selected(1, 1)
    win._extend_selection(1, 3)
    check("多选 3 张待保留", len(d.sel_set) == 3, f"got {len(d.sel_set)}")
    win._set_keep_of_selection()
    row1 = win.model.row_at(1)
    check("多选设为保留生效", {1, 2, 3} <= row1.keeps, f"got {sorted(row1.keeps)}")
    planned_paths = {p for p, _s in win.model.planned()}
    leaked = [row1.items[p].path for p in (1, 2, 3) if row1.items[p].path in planned_paths]
    check("保留的 3 张都不在待删清单里", not leaked, f"泄漏 {leaked}")
    win._clear_selected()

    import imgdedup_ui.window as W
    from PySide6.QtWidgets import QMenu

    cap = {}

    class Grab(QMenu):
        def exec(self, *a, **k):
            cap["m"] = self
            return None

    real = W.QMenu
    W.QMenu = Grab
    try:
        win._on_context_menu(vp_point(1, 4))
    finally:
        W.QMenu = real
    m = cap.get("m")
    check("右键菜单构建成功", m is not None)
    if m:
        texts = [a.text() for a in m.actions() if a.text()]
        check("菜单 8 项", len(texts) == 8, str(texts))
        check("含「立即删除这一张…」", "立即删除这一张…" in texts)
        check("含「在文件夹中显示」", "在文件夹中显示" in texts)

    click(vp_point(0, 0))
    check("选中保留项 → 标记删除被禁用", not win.btn_mark.isEnabled())
    click(vp_point(0, 2))
    check("选中普通项 → 标记删除可用", win.btn_mark.isEnabled())

    vr = win.view.visualRect(win.view.model().index(2, 0))
    card = QRect(0, 0, vr.width(), vr.height())
    pt = d._item_rect(card, 3).center() + vr.topLeft()
    click(pt)
    check("超大聚类里选中也禁用删除", not win.btn_delete_one.isEnabled())
    check("超大聚类不进入待删清单",
          all(p not in [i.path for i in win.model.row_at(2).items]
              for p, _s in win.model.planned()))

    from imgdedup_ui.widgets import PreviewPane

    win.preview.show_path(None)
    app.processEvents()
    check("缩放输入框存在且为 100%",
          win.zoom_edit.isVisible() and win.zoom_edit.text() == "100%",
          f"可见={win.zoom_edit.isVisible()} 文本={win.zoom_edit.text()!r}")

    win.zoom_edit.setText("300")
    win.zoom_edit.editingFinished.emit()
    check("输入 300 后倍率变 3.0",
          abs(win.preview.zoom - 3.0) < 1e-6, f"zoom={win.preview.zoom}")
    check("输入框回写为 300%", win.zoom_edit.text() == "300%",
          repr(win.zoom_edit.text()))

    win.zoom_edit.setText("9999")
    win.zoom_edit.editingFinished.emit()
    check("越界输入被夹到上限",
          abs(win.preview.zoom - PreviewPane.ZOOM_MAX) < 1e-6,
          f"zoom={win.preview.zoom}")
    check("框里显示夹取后的值",
          win.zoom_edit.text() == f"{PreviewPane.ZOOM_MAX:.0%}",
          repr(win.zoom_edit.text()))

    win.zoom_edit.setText("abc")
    win.zoom_edit.editingFinished.emit()
    check("乱输入不改倍率且回退显示",
          abs(win.preview.zoom - PreviewPane.ZOOM_MAX) < 1e-6
          and win.zoom_edit.text() == f"{PreviewPane.ZOOM_MAX:.0%}",
          f"zoom={win.preview.zoom} 文本={win.zoom_edit.text()!r}")

    win.preview._pan = QPointF(30.0, 20.0)
    win.btn_zoom_reset.click()
    app.processEvents()
    check("重置按钮恢复 100%",
          abs(win.preview.zoom - 1.0) < 1e-6, f"zoom={win.preview.zoom}")
    check("重置同时清掉平移",
          abs(win.preview._pan.x()) < 1e-6 and abs(win.preview._pan.y()) < 1e-6,
          f"pan={win.preview._pan}")
    check("重置后输入框显示 100%", win.zoom_edit.text() == "100%",
          repr(win.zoom_edit.text()))

    win.preview.zoom_by(PreviewPane.ZOOM_STEP)
    app.processEvents()
    check("滚轮缩放后输入框跟随",
          win.zoom_edit.text() == f"{win.preview.zoom:.0%}",
          f"zoom={win.preview.zoom} 文本={win.zoom_edit.text()!r}")

    win.zoom_edit.clear()
    win.zoom_edit.insert("15")
    win.preview.set_zoom(2.0)
    check("打字过程中不被同步冲掉",
          win.zoom_edit.text() == "15", repr(win.zoom_edit.text()))

    win.zoom_edit.clear()
    win.zoom_edit.insert("150")
    win._apply_zoom_from_edit()
    check("提交后归一为 150% 且恢复同步",
          win.zoom_edit.text() == "150%" and not win.zoom_edit.isModified(),
          f"文本={win.zoom_edit.text()!r} modified={win.zoom_edit.isModified()}")

    win.zoom_edit.setFocus()
    app.processEvents()
    win.preview.set_zoom(4.0)
    check("只聚焦未打字时滚轮照常回写",
          win.zoom_edit.text() == "400%",
          f"文本={win.zoom_edit.text()!r}（用 hasFocus 判据会停在这里）")
    win.zoom_edit.clearFocus()
    win.preview.reset_zoom()

    check("缩放控件在预览区那一格里",
          win.right_split.count() == 2
          and win.right_split.widget(0).isAncestorOf(win.zoom_edit),
          f"splitter 格数={win.right_split.count()}")

    import tempfile
    from pathlib import Path

    from PIL import Image

    tmp = tempfile.TemporaryDirectory()
    gifs = []
    for k in range(2):
        frames = [Image.new("RGB", (80, 60), (30 + 60 * k, 90, 200))
                  for _ in range(8)]
        gp = Path(tmp.name) / f"anim{k}.gif"
        frames[0].save(str(gp), save_all=True, append_images=frames[1:],
                       duration=40, loop=0)
        gifs.append(str(gp))

    for i in range(16):
        win.preview.show_path(gifs[i % 2])
    n_tasks = len(win.preview._anim_tasks)
    check("快速切 16 次 GIF 不泄漏解码线程",
          n_tasks <= 2, f"_anim_tasks 堆了 {n_tasks} 个（切一次涨一个）")

    win.preview.shutdown_anim()
    check("收工后没有在途解码线程",
          len(win.preview._anim_tasks) == 0
          and not any(t.isRunning() for t in win.preview._anim_tasks),
          f"还剩 {len(win.preview._anim_tasks)} 个")
    tmp.cleanup()
    win.preview.show_path(None)

    _log("=" * 58)
    _log("  全部通过 ✅" if not FAIL else f"  {len(FAIL)} 项失败 ❌")
    _log("=" * 58)

    out = os.environ.get("IMGDEDUP_CLICK_OUT")
    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"ok": not FAIL, "lines": LINES}, fh,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass
    for line in LINES:
        try:
            print(line, flush=True)
        except Exception:
            pass
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
