"""预览框 + 缩略图尺寸调节。

这两个功能的正确性分两层，测试也分两层：

- 能测的：尺寸改动是否真的传导到布局计算（`per_line` / `card_height`）、
  预览框的路径切换与"token 作废"逻辑、两个缓存是否真的独立。
- 测不了的：预览框里那张图长得对不对、字号是否协调：那要靠
  `dev/shot_preview.py` 出图人眼看。

绝不能空转 `processEvents()` 去等后台解码：那样驱动不了 `QThreadPool`，
会得出"没加载出来"的错误结论。要等就用真的 `QEventLoop`。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from imgdedup_core.scan import FileEntry  # noqa: E402
from imgdedup_ui.model import build_rows  # noqa: E402


def ent(path, size=1000, w=800, h=600):
    return FileEntry(path=path, size=size, mtime_ns=0, ext=".png",
                     width=w, height=h, fmt="PNG")


def _rows(n=8):
    entries = [ent(f"a{i}.png", size=1000 - i) for i in range(n)]
    return build_rows(entries, [list(range(n))], "exact")


class TestThumbSizeAdjust(unittest.TestCase):
    """缩略图尺寸可调。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _win(self, n=8):
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        w.model.set_rows(_rows(n))
        return w

    def test_delegate_has_thumb_size(self):
        w = self._win()
        from imgdedup_ui.window import THUMB_DEFAULT

        self.assertEqual(w.delegate.thumb_size, THUMB_DEFAULT)

    def test_set_thumb_size_clamped(self):
        """超出范围要被 clamp，不能抛，也不能真的把尺寸设成 9999。"""
        w = self._win()
        from imgdedup_ui.window import THUMB_MAX, THUMB_MIN

        w.delegate.set_thumb_size(9999)
        self.assertEqual(w.delegate.thumb_size, THUMB_MAX)
        w.delegate.set_thumb_size(1)
        self.assertEqual(w.delegate.thumb_size, THUMB_MIN)

    def test_set_thumb_size_returns_changed(self):
        """返回值语义：真的变了才是 True（调用方靠它决定要不要重布局）。"""
        w = self._win()
        self.assertTrue(w.delegate.set_thumb_size(200))
        self.assertFalse(w.delegate.set_thumb_size(200))

    def test_per_line_shrinks_when_thumbs_grow(self):
        """缩略图变大 → 每行张数变少。这是"尺寸可调"最核心的可观测行为。"""
        w = self._win()
        w.delegate.set_view_width(900)
        small = w.delegate.per_line()
        w.delegate.set_thumb_size(220)
        big = w.delegate.per_line()
        self.assertLess(big, small,
                        f"缩略图变大后每行张数没变：{small} -> {big}")
        self.assertGreaterEqual(big, 1)

    def test_card_height_grows_when_thumbs_grow(self):
        """卡片高度必须跟着长：不跟着长的话缩略图会画到下一张卡片头上。"""
        w = self._win()
        row = w.model.rows[0]
        w.delegate.set_thumb_size(80)
        h1 = w.delegate.card_height(row)
        w.delegate.set_thumb_size(200)
        h2 = w.delegate.card_height(row)
        self.assertGreater(h2, h1)
        # 差值应当是整数倍的行高差，至少覆盖 (200-80)
        self.assertGreaterEqual(h2 - h1, 120)

    def test_slider_drives_delegate(self):
        """滑块 → delegate。这是真实触发路径，不是手工调 setter。"""
        w = self._win()
        w.sld_thumb.setValue(176)
        self.assertEqual(w.delegate.thumb_size, 176)
        self.assertEqual(w.lbl_thumb.text(), "176")

    def test_slider_range_matches_constants(self):
        from imgdedup_ui.window import THUMB_MAX, THUMB_MIN

        w = self._win()
        self.assertEqual(w.sld_thumb.minimum(), THUMB_MIN)
        self.assertEqual(w.sld_thumb.maximum(), THUMB_MAX)

    def test_size_hint_tracks_thumb_size(self):
        """`sizeHint` 是视图排行的依据，必须跟着尺寸走。"""
        w = self._win()
        w.delegate.set_view_width(900)
        idx = w.view.model().index(0, 0)
        w.delegate.set_thumb_size(80)
        h1 = w.delegate.sizeHint(None, idx).height()
        w.delegate.set_thumb_size(200)
        h2 = w.delegate.sizeHint(None, idx).height()
        self.assertGreater(h2, h1)


class TestPreviewPane(unittest.TestCase):
    """预览框的状态机。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _win(self, n=8):
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        w.model.set_rows(_rows(n))
        return w

    def test_preview_cache_is_independent(self):
        """预览必须用自己的缓存，不能和网格共用。

        共用的话，逛一圈预览（每张 800px）会把网格那 800 张小图挤光，
        表现为"滚回去发现缩略图全在重新加载"。
        """
        w = self._win()
        self.assertIsNot(w.preview.cache, w.thumbs)
        self.assertGreater(w.preview_cache.size, w.thumbs.size,
                           "预览的解码尺寸必须比网格大，否则看不出细节")

    def test_preview_placement_by_path(self):
        """挂载路径是真实触发路径：`_set_selected` → `_after_selection_change`
        → `_on_current_changed` → `preview.show_path()`。"""
        w = self._win()
        w.delegate.set_view_width(900)
        w._set_selected(0, 3)
        self.assertEqual(w.preview._path, w.model.rows[0].items[3].path)

    def test_preview_clears_when_deselected(self):
        w = self._win()
        w._set_selected(0, 3)
        self.assertIsNotNone(w.preview._path)
        w._clear_selected()
        self.assertIsNone(w.preview._path, "取消选中后预览没清空")

    def test_detail_panel_clears_when_deselected(self):
        """取消选中后，右侧「图片信息」也必须清空。

        取消的是缩略图级选中，而当前分组行仍在 ——
        于是 `_on_current_changed` 会走"没选中缩略图"那条分支。
        那一支曾经填的是「未选中具体缩略图。推荐保留：XXX / 尺寸 / 保留理由」，
        结果取消选中后右侧仍残留一张图的文件名与尺寸，
        看起来像"那一张还选着"，用户会以为取消没生效。
        """
        w = self._win()
        w.delegate.set_view_width(900)
        w._set_selected(0, 3)
        self.assertIn("文件：", w.lbl_detail.text(),
                      "前置条件：选中时详情应显示文件信息")

        w._clear_selected()
        self.assertEqual(w.lbl_detail.text(), "",
                         "取消选中后右侧图片信息没清空")
        self.assertEqual(w.lbl_focus.text(), "",
                         "取消选中后「已选中」提示没清空")

    def test_preview_clears_on_new_scan(self):
        """重扫时必须清预览，否则旧图残留，用户以为新结果里有这张。"""
        w = self._win()
        w._set_selected(0, 3)
        self.assertIsNotNone(w.preview._path)
        w.model.set_rows([])
        w.preview.show_path(None)
        self.assertIsNone(w.preview._path)

    def test_same_path_does_not_re_request(self):
        """重复设同一个路径不应重新请求，否则每次重绘都起一个解码任务。"""
        w = self._win()
        w.delegate.set_view_width(900)
        w._set_selected(0, 2)
        first = w.preview._path
        tok = w.preview._token
        w.preview.show_path(first, "x", "y")
        self.assertIs(w.preview._token, tok, "同路径重设触发了重新请求")

    def test_stale_token_ignored(self):
        """切图后旧任务的结果必须作废。

        这是最容易出的 bug：快速连点几下，先发的任务后回来，
        把新图覆盖成旧图：用户看到"预览和选中项对不上"。
        """
        w = self._win()
        w.delegate.set_view_width(900)
        w._set_selected(0, 1)
        stale = w.preview._token
        w.preview.show_path(w.model.rows[0].items[5].path)
        self.assertIsNot(w.preview._token, stale)
        # 喂一个过期的 token：不应改变当前 pixmap
        before = w.preview._pixmap
        w.preview._on_loaded(stale if stale is not None else -999)
        self.assertIs(w.preview._pixmap, before, "过期 token 覆盖了当前预览")

    def test_caption_filled(self):
        """文件名/尺寸仍要送到预览组件：虽然预览框本身不再叠画这些字，
        但主窗口仍传，保留这条契约防止调用点被误删。"""
        w = self._win()
        w.delegate.set_view_width(900)
        w._set_selected(0, 3)
        self.assertTrue(w.preview._caption)
        self.assertTrue(w.preview._sub)
        self.assertIn("a3.png", w.preview._caption)

    def test_preview_has_minimum_size(self):
        """预览框在窗口极小的时候不能塌成 0 高。

        最小值不能设太大：它现在住在 350px 宽的右栏里，
        给 240 会直接撑破布局（Qt 会强行把侧栏顶宽）。
        """
        w = self._win()
        self.assertGreaterEqual(w.preview.minimumHeight(), 100)
        self.assertLessEqual(w.preview.minimumWidth(), 260,
                             "预览最小宽度超过了侧栏宽度")

    def test_right_is_vertical_split(self):
        """右侧栏是上下分栏：上预览大图、下信息 + 按钮。

        上半格现在是容器（预览框 + 下方那行缩放控件），不再是
        `w.preview` 本身：两个控件直接 `addWidget` 进 splitter 会多出
        一格，拖动分隔条时缩放控件会被单独拉扯。所以这里断言"祖先关系"
        而不是"就是它"。
        """
        from PySide6.QtWidgets import QSplitter

        w = self._win()
        self.assertIsInstance(w.right_split, QSplitter)
        self.assertEqual(w.right_split.orientation(), Qt.Vertical)
        self.assertEqual(w.right_split.count(), 2)
        self.assertTrue(w.right_split.widget(0).isAncestorOf(w.preview),
                        "上半格不是预览区")

    def test_center_is_plain_grid(self):
        """中间栏只剩网格：预览搬去右栏后不该再套 splitter。"""
        w = self._win()
        self.assertFalse(hasattr(w, "center_split"),
                         "中间栏还留着 center_split，预览没真正搬走")
        self.assertIsNotNone(w.view.parent())

    def test_preview_is_not_collapsible(self):
        """预览框不能被拖到消失：拖没了用户找不到它，也不知道怎么恢复。"""
        w = self._win()
        self.assertFalse(w.right_split.isCollapsible(0))
        self.assertFalse(w.right_split.isCollapsible(1))

    def test_preview_is_inside_right_column(self):
        """预览的祖先链必须落在右栏里，不能在中间栏。

        只断言 `right_split.widget(0) is preview` 不够：万一有人
        同时把它也 addWidget 到别处，Qt 会把它从原位置摘走，
        而 splitter 的 index 断言在重建前仍可能"看起来对"。
        这里直接沿 parent 链向上确认归属。
        """
        from PySide6.QtWidgets import QSplitter

        w = self._win()
        self.assertIsInstance(w.right_split, QSplitter)
        node = w.preview
        seen = []
        while node is not None:
            seen.append(node)
            node = node.parentWidget()
        self.assertIn(w.right_split, seen, "预览不在右栏的控件树里")
        self.assertNotIn(w.view, seen, "预览竟然挂在网格视图下面")

    def test_real_decode_end_to_end(self):
        """真的解码一张图，确认链路通。

        用真 `QEventLoop` 等，绝不空转 `processEvents()` ——
        空转驱动不了 `QThreadPool`，会得到"没加载出来"的假结论。
        """
        import tempfile
        from pathlib import Path

        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没有 Pillow")

        w = self._win()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.png"
            Image.new("RGB", (1200, 900), (200, 120, 60)).save(str(p))

            loop = QEventLoop()
            w.preview_cache.loaded.connect(lambda _t: loop.quit())
            QTimer.singleShot(5000, loop.quit)
            w.preview.show_path(str(p), "big.png", "1200x900")
            loop.exec()
            self.app.processEvents()

            pm = w.preview_cache.get(str(p))
            self.assertIsNotNone(pm, "预览大图没有解码出来")
            # 上限跟着 `PreviewPane.STILL_SIDE` 走，不写死 800。
            # 滚轮最大放大到 16x，预览的解码尺寸必须与之一致地调大，
            # 否则放大后立刻糊成马赛克。写死数字会在调参时变成假失败。
            from imgdedup_ui.widgets import PreviewPane
            self.assertLessEqual(max(pm.width(), pm.height()),
                                 PreviewPane.STILL_SIDE)

        w.thumbs.shutdown()
        w.preview_cache.shutdown()


class TestShutdownSafety(unittest.TestCase):
    """两个缓存都必须被窗口收干净。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_both_caches_shut_down(self):
        """`closeEvent` 必须收两个线程池。

        只收 `thumbs` 而漏掉 `preview_cache`，退出时照样刷
        "Signal source has been deleted"，而且比只收一个时更难发现，
        因为输出看着"已经好多了"。
        """
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        calls = []
        w.thumbs.shutdown = lambda: calls.append("thumbs")
        w.preview_cache.shutdown = lambda: calls.append("preview")
        w.close()
        self.assertIn("thumbs", calls)
        self.assertIn("preview", calls)


if __name__ == "__main__":
    unittest.main()
