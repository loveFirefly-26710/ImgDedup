"""下拉框与即时提示。

两件事在 QSS 上做不到，只能靠自绘与自建窗口：
- 下拉弹出列表的选中行不采纳 `::item:selected`，也不采纳 palette.Highlight；
- 提示气泡要「常驻不自动消失」，所以不能用 `QToolTip`（它会自己超时）。
"""

import unittest

from tests import _qt

from imgdedup_ui import theme as T
from imgdedup_ui import widgets as W
from imgdedup_ui import window as Win
from imgdedup_ui import worker as Wk


class ComboCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _qt.app()

    def setUp(self):
        self.win = Win.MainWindow()

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        _qt.flush()


class TestComboItems(ComboCase):

    def test_theme_combo_lists_every_preset_once(self):
        labels = [self.win.cmb_theme.itemText(i)
                  for i in range(self.win.cmb_theme.count())]
        self.assertEqual(len(labels), len(T.THEMES))
        self.assertEqual(len(set(labels)), len(labels))
        self.assertEqual([ln for ln in labels if not ln.strip()], [],
                         "下拉里出现了空项 —— 用户会把它读成「空选项」")

    def test_mode_combo_lists_every_mode(self):
        labels = [self.win.cmb_mode.itemText(i)
                  for i in range(self.win.cmb_mode.count())]
        self.assertEqual(len(labels), len(Wk.MODE_ORDER))
        self.assertEqual([ln for ln in labels if not ln.strip()], [])

    def test_theme_labels_are_the_preset_names(self):
        got = {self.win.cmb_theme.itemText(i)
               for i in range(self.win.cmb_theme.count())}
        want = {t["name"] for t in T.THEMES.values()}
        self.assertEqual(got, want)


class TestComboDelegate(ComboCase):

    def test_combos_use_the_custom_delegate(self):
        """选中行色只能自绘 —— QSS 与 palette 都被 Qt 忽略掉了。"""
        for name in ("cmb_theme", "cmb_mode"):
            combo = getattr(self.win, name)
            with self.subTest(combo=name):
                self.assertIsInstance(combo.itemDelegate(), W.ComboItemDelegate)

    def test_delegate_paints_a_real_index(self):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtWidgets import QStyleOptionViewItem

        combo = self.win.cmb_theme
        index = combo.model().index(0, 0)
        self.assertTrue(index.isValid())

        pm = QPixmap(160, 24)
        painter = QPainter(pm)
        try:
            opt = QStyleOptionViewItem()
            opt.rect = QRect(0, 0, 160, 24)
            combo.itemDelegate().paint(painter, opt, index)
        finally:
            painter.end()
        self.assertFalse(pm.isNull())


class TestInstantToolTip(ComboCase):

    def _tip(self, text):
        tip = W.InstantToolTip(self.win)
        self.addCleanup(tip.deleteLater)
        self.addCleanup(tip.dismiss)
        tip.attach(self.win.cmb_theme, text)
        return tip

    def test_tooltip_stays_until_dismissed(self):
        """即时提示要常驻：`QToolTip` 会自己超时消失，所以这里是自己画的窗口。"""
        tip = self._tip("说明文字")
        self.assertFalse(tip.isVisible())
        tip.popup_for(self.win.cmb_theme)
        _qt.flush()
        self.assertTrue(tip.isVisible())
        tip.dismiss()
        _qt.flush()
        self.assertFalse(tip.isVisible())

    def test_empty_text_does_not_pop_up(self):
        tip = self._tip("")
        tip.popup_for(self.win.cmb_theme)
        _qt.flush()
        self.assertFalse(tip.isVisible())

    def test_whitespace_only_text_does_not_pop_up(self):
        tip = self._tip("   \n  ")
        tip.popup_for(self.win.cmb_theme)
        _qt.flush()
        self.assertFalse(tip.isVisible())

    def test_provider_may_be_a_callable(self):
        state = {"text": "动态文本"}
        tip = self._tip(lambda: state["text"])
        tip.popup_for(self.win.cmb_theme)
        _qt.flush()
        self.assertTrue(tip.isVisible())
        # 清空之后再次弹，应当什么都不显示
        state["text"] = ""
        tip.popup_for(self.win.cmb_theme)
        _qt.flush()
        self.assertFalse(tip.isVisible())

    def test_attach_records_the_owner(self):
        tip = self._tip("x")
        self.assertIn(tip, getattr(self.win.cmb_theme, "_instant_tooltips", []))


class TestTooltipFilter(ComboCase):

    def test_window_owns_a_shared_tooltip(self):
        # 全窗口共用同一个气泡实例，`tip_filter` 就是它
        self.assertIsInstance(self.win.tip_filter, W.InstantToolTip)


if __name__ == "__main__":
    unittest.main()
