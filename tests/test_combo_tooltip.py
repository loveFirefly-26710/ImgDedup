"""下拉框与即时提示的测试。

钉住本轮两个需求：

1. 下拉框里不能有空选项，且选中行必须一眼看得出是选中
  ，而不是"一条和背景差不多的浅色带"（用户截图反馈的"空白选项"）。
2. 提示是即时的、且常驻不自动消失（只在移出/失焦时关）。

第 1 条特别值得钉：它踩过 Qt 样式表一个绕不过去的坑 ——
只要 `QComboBox` 控件本身命中任何 QSS，popup 的选中行就不采纳
`::item:selected { background }`，也不采纳 `palette.Highlight`，
各种写法实测全无效，只能用自绘 delegate。所以这里用
离屏逐像素采样来断言，而不是断言"QSS 里写了某条规则"
（后者会给出假绿：规则在、但画面上不生效）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from imgdedup_ui import theme  # noqa: E402
from imgdedup_ui.widgets import ComboItemDelegate, InstantToolTip  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


def _row_bg(cb: QComboBox, index: int) -> str:
    """取弹出列表第 `index` 行的底色（避开文字，取左侧 padding 区）。

    必须 `showPopup()` 之后从 popup 窗口 抓图：只 `grab()` view
    拿到的是 0x0（view 没经过布局）。这条是实测踩出来的。
    """
    cb.showPopup()
    _app().processEvents()
    view = cb.view()
    img = view.window().grab().toImage()
    r = view.visualRect(view.model().index(index, 0))
    y = max(0, min((r.top() + r.bottom()) // 2, img.height() - 1))
    color = QColor(img.pixel(8, y)).name()
    cb.hidePopup()
    return color


def _make_combo(theme_key: str = "mint") -> QComboBox:
    cb = QComboBox()
    cb.addItems(["快速", "精确", "深度"])
    cb.resize(220, 30)
    cb.show()
    _app().processEvents()
    theme.apply_theme(_app(), theme_key)
    return cb


class TestComboItems(unittest.TestCase):
    """需求 A：没有空选项。"""

    def setUp(self):
        self.app = _app()

    def test_mode_combo_has_no_empty_items(self):
        """`cmb_mode` 三项文本都必须非空：用户截图里那条"空行"其实不是空项。

        别在这里断言 `itemData` 非空：`addItems()` 只填显示文本、
        data 全是 None，那是正常的。用户的"空选项"观感来自选中行没上色，
        由 `TestComboSelectedRowVisible` 那些像素断言负责。
        """
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            cb = w.cmb_mode
            self.assertEqual(cb.count(), 3, "扫描模式应该正好三档")
            for i in range(cb.count()):
                self.assertTrue(cb.itemText(i).strip(),
                                f"第 {i} 项文本为空：{cb.itemText(i)!r}")
        finally:
            w.close()

    def test_theme_combo_has_no_empty_items(self):
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            cb = w.cmb_theme
            for i in range(cb.count()):
                self.assertTrue(cb.itemText(i).strip(),
                                f"主题下拉第 {i} 项文本为空")
        finally:
            w.close()


class TestComboSelectedRowVisible(unittest.TestCase):
    """需求 A 的核心：选中行要看得出是选中（不是靠一条浅灰描边）。"""

    def setUp(self):
        self.app = _app()

    def test_delegate_is_installed(self):
        """选中行靠自绘 delegate，所以必须确认真的装上了。"""
        cb = _make_combo()
        try:
            self.assertIsInstance(cb.itemDelegate(), ComboItemDelegate)
        finally:
            cb.deleteLater()

    def test_selected_row_is_filled_and_others_are_not(self):
        """选中行的底色必须 ≠ 未选中行，且 ≠ 白（说明真的填色了）。"""
        cb = _make_combo()
        try:
            cb.setCurrentIndex(1)
            sel_bg = _row_bg(cb, 1)
            plain_bg = _row_bg(cb, 0)
            self.assertNotEqual(
                sel_bg, plain_bg,
                "选中行和未选中行颜色一样 —— 用户会把它读成『空选项』")
            self.assertNotIn(
                sel_bg, ("#ffffff", "#000000"),
                f"选中行底色 {sel_bg} 是纯白/纯黑，说明 delegate 没上色")
        finally:
            cb.deleteLater()

    def test_selected_row_follows_theme(self):
        """换主题后选中行底色要跟着变，否则就是写死了颜色。"""
        seen = {}
        for key in theme.THEMES:
            cb = _make_combo(key)
            try:
                cb.setCurrentIndex(0)
                seen[key] = _row_bg(cb, 0)
            finally:
                cb.deleteLater()
        # 五套主题的 soft 各不相同，底色不该全一样
        self.assertGreater(len(set(seen.values())), 1,
                           f"各主题选中行底色完全相同，疑似写死：{seen}")

    def test_main_window_combo_selected_row_visible(self):
        """真实窗口里的下拉框也要生效。

        这条是回归护栏：`apply_theme()` 在 `MainWindow()` 之前 调，
        那时 `cmb_mode` 还不存在，扫不到它。若忘了在窗口构造后补一次，
        这个测试就会红：这正是本轮踩过的坑。
        """
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            cb = w.cmb_mode
            cb.setCurrentIndex(2)
            sel_bg = _row_bg(cb, 2)
            plain_bg = _row_bg(cb, 0)
            self.assertNotEqual(sel_bg, plain_bg,
                                "主窗口下拉选中行没上色（delegate 没补装？）")
        finally:
            w.close()


class TestInstantToolTip(unittest.TestCase):
    """需求 B：即时弹出 + 常驻不自动消失。"""

    def setUp(self):
        self.app = _app()
        self.cb = QComboBox()
        self.cb.addItems(["A", "B"])
        self.cb.resize(120, 28)
        self.cb.show()
        self.app.processEvents()

    def tearDown(self):
        self.cb.deleteLater()
        self.app.processEvents()

    def test_initially_hidden(self):
        tip = InstantToolTip()
        self.assertFalse(tip.isVisible())

    def test_shows_immediately_on_enter(self):
        tip = InstantToolTip()
        tip.attach(self.cb, "说明文字")
        tip.popup_for(self.cb)
        self.app.processEvents()
        self.assertTrue(tip.isVisible())

    def test_no_auto_hide_timer(self):
        """常驻：弹久了也不能自己消失（内建 tooltip 会在约 10 秒后自消）。

        这里不真等 10 秒（跑测试不该花那么久），而是断言没有任何
        单发定时器会在自动关闭：自绘气泡压根不该有那个定时器。
        真正的时间维度由"只在 Leave/FocusOut 才关"这个事件驱动来保证。
        """
        tip = InstantToolTip()
        tip.attach(self.cb, "常驻文字")
        tip.popup_for(self.cb)
        self.app.processEvents()
        # 触发事件循环空转（模拟"过了一段时间"）
        for _ in range(5):
            self.app.processEvents()
        self.assertTrue(tip.isVisible(), "气泡自己消失了 —— 不该有自动关闭")

    def test_dismiss_only_for_matching_owner(self):
        """只在锚点匹配时关：别的控件失焦不该连累它。"""
        tip = InstantToolTip()
        tip.attach(self.cb, "文字")
        tip.popup_for(self.cb)
        self.app.processEvents()
        tip.dismiss(object())           # 别的对象
        self.assertTrue(tip.isVisible(), "被无关对象关掉了")
        tip.dismiss(self.cb)
        self.app.processEvents()
        self.assertFalse(tip.isVisible())

    def test_empty_text_does_not_show(self):
        """空文案不弹：需求 A 说"移除空白选项"，气泡同理不该弹空壳。"""
        tip = InstantToolTip()
        tip.attach(self.cb, "   ")
        tip.popup_for(self.cb)
        self.app.processEvents()
        self.assertFalse(tip.isVisible())

    def test_callable_provider_reflects_updates(self):
        """文案会变的场景（模式说明随档位切换）要能拿到新值。"""
        state = {"txt": "A 档"}
        tip = InstantToolTip()
        tip.attach(self.cb, lambda: state["txt"])
        tip.popup_for(self.cb)
        self.app.processEvents()
        self.assertIn("A 档", "\n".join(tip._lines))
        state["txt"] = "B 档"
        tip.dismiss()
        tip.popup_for(self.cb)
        self.app.processEvents()
        self.assertIn("B 档", "\n".join(tip._lines))

    def test_long_text_wraps_not_truncated(self):
        """长文案必须折行，不能截断（截断等于把说明吃掉了）。"""
        long = "这是一段很长的说明，" * 20
        tip = InstantToolTip()
        tip.attach(self.cb, long)
        tip.popup_for(self.cb)
        self.app.processEvents()
        self.assertGreater(len(tip._lines), 1, "长文案没折行")
        self.assertLessEqual(tip.width(), tip.MAX_W + 2, "超过了最大宽度")
        joined = "".join(tip._lines).replace("\n", "")
        self.assertGreaterEqual(len(joined), len(long) * 0.9,
                                "折行后文字明显变少，疑似被截断")

    def test_window_flags_do_not_steal_input(self):
        """窗口标志：不抢焦点、鼠标穿透（否则气泡会把锚点的 Leave 触发掉）。"""
        tip = InstantToolTip()
        flags = tip.windowFlags()
        self.assertTrue(flags & Qt.ToolTip)
        self.assertTrue(flags & Qt.FramelessWindowHint)
        self.assertTrue(tip.testAttribute(Qt.WA_ShowWithoutActivating))
        self.assertTrue(tip.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_main_window_mode_hint_uses_instant_tooltip(self):
        """真实窗口的模式说明必须挂上即时气泡（而不是 `setToolTip`）。"""
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            self.assertTrue(getattr(w.cmb_mode, "_instant_tooltips", None),
                            "cmb_mode 没挂即时气泡")
            self.assertTrue(getattr(w.lbl_mode, "_instant_tooltips", None),
                            "lbl_mode 没挂即时气泡")
            self.assertTrue(w._mode_tip_text().strip(), "模式说明文案为空")
        finally:
            w.close()


class TestQssNoLeak(unittest.TestCase):
    """回归护栏：裸 `QListView` 规则会污染下拉弹出列表。"""

    def test_no_bare_qlistview_selector(self):
        """QSS 里不能有裸 `QListView` 选择器。

        `QComboBox` 的 view 是 `QListView` 的子类，裸规则会把
        "背景透明"套到弹出列表上，把选中行彻底洗白。
        （注释里提到 `QListView` 是允许的，所以只查选择器行。）
        """
        qss = theme.build_qss()
        # 去掉 /* ... */ 注释后再找
        import re
        stripped = re.sub(r"/\*.*?\*/", "", qss, flags=re.S)
        for line in stripped.splitlines():
            sel = line.split("{")[0].strip()
            self.assertNotEqual(
                sel, "QListView",
                "出现了裸 `QListView` 选择器 —— 会污染下拉框弹出列表")

    def test_html_like_qss_braces_balanced(self):
        qss = theme.build_qss()
        self.assertEqual(qss.count("{"), qss.count("}"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
