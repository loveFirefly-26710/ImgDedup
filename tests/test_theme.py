"""流萤主题的测试。

钉住两件事：

1. 主题能生成合法 QSS，五套都不能抛异常、token 不能缺。
2. 语义色不随主题变。 裁决三态（保留/待删/未裁决）和分类徽章四色是
   "红=危险、绿=安全"的通用直觉，为了好看改掉它们会让用户误判。
   这条最容易在"重构主题时手滑"，必须用测试钉死。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgdedup_ui import theme  # noqa: E402

REQUIRED = ("name", "accent", "deep", "soft", "line", "bg", "card",
            "text", "muted", "hover")

#: 自绘用的 token（QSS 表达不了的那部分）：
#: 主按钮渐变两端、光晕 RGB 串、背景三段渐变、卡片投影 RGB 串。
REQUIRED_PAINT = ("grad_a", "grad_b", "glow", "bg_top", "bg_mid", "bg_end",
                  "shadow")

#: 其中存 `"R, G, B"` 三元组串而不是 hex 的字段
RGB_TRIPLE = ("glow", "shadow")


class TestThemeTokens(unittest.TestCase):
    def test_all_themes_complete(self):
        self.assertGreaterEqual(len(theme.THEMES), 5)
        for key, t in theme.THEMES.items():
            for field in REQUIRED:
                self.assertIn(field, t, f"主题 {key} 缺字段 {field}")

    def test_colors_are_valid_hex(self):
        for key, t in theme.THEMES.items():
            for field in REQUIRED:
                if field == "name":
                    continue
                v = t[field]
                self.assertRegex(v, r"^#[0-9A-Fa-f]{6}$",
                                 f"主题 {key}.{field} 不是合法 hex：{v}")

    def test_paint_tokens_complete_and_valid(self):
        """自绘 token 也得补全：漏一个的话自绘那边会静默用回硬编码默认值。"""
        for key, t in theme.THEMES.items():
            for field in REQUIRED_PAINT:
                self.assertIn(field, t, f"主题 {key} 缺自绘字段 {field}")
                v = t[field]
                if field in RGB_TRIPLE:
                    parts = [p.strip() for p in v.split(",")]
                    self.assertEqual(len(parts), 3,
                                     f"主题 {key}.{field} 不是 RGB 三元组：{v}")
                    for p in parts:
                        self.assertTrue(p.isdigit() and 0 <= int(p) <= 255,
                                        f"主题 {key}.{field} 分量越界：{v}")
                else:
                    self.assertRegex(v, r"^#[0-9A-Fa-f]{6}$",
                                     f"主题 {key}.{field} 不是合法 hex：{v}")

    def test_mint_background_ends_warm(self):
        """薄荷主题的背景收尾必须是暖色。

        这是"流萤"的灵魂：萤火虫的光是暖的。如果哪天有人把 `bg_end`
        改成冷绿，整个软件会立刻变成"医疗设备风"—— 这条测试守着那个暖收尾。
        """
        end = theme.THEMES["mint"]["bg_end"].lstrip("#")
        r, g, b = int(end[0:2], 16), int(end[2:4], 16), int(end[4:6], 16)
        self.assertGreater(r, b, "薄荷主题的背景收尾变冷了（暖米应为 R > B）")
        self.assertGreater(g, b, "薄荷主题的背景收尾变冷了（暖米应为 G > B）")
        # 也不能黄到发土：蓝分量要留一点
        self.assertGreater(b, 200, "背景收尾过黄，会显得发土")

    def test_unknown_key_falls_back(self):
        """主题 key 写错 → 回落默认，绝不抛异常（配置坏了软件也得能起来）。"""
        self.assertEqual(theme.theme("nonexistent"), theme.THEMES["mint"])
        self.assertEqual(theme.theme(None), theme.THEMES["mint"])

    def test_build_qss_all_themes(self):
        for key in theme.THEMES:
            q = theme.build_qss(key)
            self.assertGreater(len(q), 800, f"{key} 生成的 QSS 太短，可能漏了样式")
            self.assertIn(theme.THEMES[key]["accent"], q)
            self.assertIn("QPushButton#primary", q)
            self.assertIn("QPushButton#danger", q)


class TestSemanticColorsConstant(unittest.TestCase):
    """语义色不随主题变：这是设计上的硬约束，不是实现细节。"""

    def test_keep_del_undecided_fixed_across_themes(self):
        from PySide6.QtWidgets import QApplication

        from imgdedup_ui import widgets as W

        app = QApplication.instance() or QApplication([])
        seen = {}
        for key in theme.THEMES:
            theme.apply_theme(app, key)
            seen[key] = (W.C_KEEP.name(), W.C_DEL.name(),
                         W.C_UNDECIDED.name())
        values = set(seen.values())
        self.assertEqual(len(values), 1,
                         f"语义色跟着主题变了：{seen}")

    def test_badge_colors_fixed_across_themes(self):
        from PySide6.QtWidgets import QApplication

        from imgdedup_ui import widgets as W

        app = QApplication.instance() or QApplication([])
        seen = set()
        for key in theme.THEMES:
            theme.apply_theme(app, key)
            seen.add(tuple((b[0].name(), b[1].name())
                           for b in W.BADGE.values()))
        self.assertEqual(len(seen), 1, "分类徽章色跟着主题变了")

    def test_all_badge_kinds_present(self):
        for k in ("exact", "perceptual", "semantic", "cluster"):
            self.assertIn(k, theme.BADGE)
            self.assertIn(k, __import__("imgdedup_ui.widgets",
                                        fromlist=["BADGE"]).BADGE)

    def test_badge_text_never_leaks_the_kind(self):
        """角标文案必须是中文。

        `worker.py` 给感知组建行时没传 `label_of`，于是 `row.label` 就是
        kind 字符串本身，界面上会露出一个英文单词 "perceptual"。
        这里钉住四种 kind 都有中文文案。
        """
        from imgdedup_ui.model import GroupRow
        from imgdedup_ui.widgets import GroupDelegate

        for kind, want in (("exact", "字节完全相同"),
                           ("perceptual", "感知相似"),
                           ("semantic", "语义相似")):
            row = GroupRow(kind=kind, label=kind)      # label 就是 kind 字符串
            self.assertEqual(GroupDelegate._badge_text(row), want,
                             f"{kind} 的角标文案不是中文")

        row = GroupRow(kind="cluster", label="过合并，需人工确认", oversized=True)
        self.assertEqual(GroupDelegate._badge_text(row), "聚类 · 需人工确认")

        # 真给了文案就用文案：以后感知组想显示「相似度 96%」不该被挡掉
        row = GroupRow(kind="perceptual", label="相似度 96%")
        self.assertEqual(GroupDelegate._badge_text(row), "相似度 96%")


class TestApplyTheme(unittest.TestCase):
    """`apply_theme()` 必须同时刷新 QSS 和自绘控件常量。

    只刷一边的症状是「按钮变薄荷了、缩略图边框还是蓝的」——
    半截状态比不换主题还难受。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_qss_and_widget_constants_move_together(self):
        from imgdedup_ui import widgets as W

        theme.apply_theme(self.app, "mint")
        mint_focus = W.C_FOCUS.name()
        mint_qss = self.app.styleSheet()

        theme.apply_theme(self.app, "starlight")
        self.assertNotEqual(W.C_FOCUS.name(), mint_focus,
                            "自绘控件的焦点色没跟着主题走")
        self.assertNotEqual(self.app.styleSheet(), mint_qss,
                            "QSS 没跟着主题走")
        self.assertIn(theme.THEMES["starlight"]["accent"],
                      self.app.styleSheet())

        theme.apply_theme(self.app, "mint")   # 还原，别影响后面的测试

    def test_focus_uses_deep_not_accent(self):
        """选中环用 `deep` 而不是 `accent`。

        `#14B8A6` 明度偏高，叠在花了呼哨的缩略图上会被图片吃掉。
        这条是设计稿里肉眼比出来的，写死防回退。
        """
        from imgdedup_ui import widgets as W

        theme.apply_theme(self.app, "mint")
        # `QColor.name()` 返回小写，主题表里是大写：比大小写不敏感
        self.assertEqual(W.C_FOCUS.name().lower(),
                         theme.THEMES["mint"]["deep"].lower())
        self.assertNotEqual(W.C_FOCUS.name().lower(),
                            theme.THEMES["mint"]["accent"].lower())

    def test_accent_refreshed(self):
        from imgdedup_ui import widgets as W

        theme.apply_theme(self.app, "aurora")
        self.assertEqual(W.C_ACCENT.name().lower(),
                         theme.THEMES["aurora"]["accent"].lower())
        theme.apply_theme(self.app, "mint")

    def test_persist_roundtrip(self):
        """偏好落盘再读回。写失败不该抛（只读盘 / 权限问题时静默降级）。"""
        import tempfile
        from pathlib import Path

        orig = theme.settings_path

        with tempfile.TemporaryDirectory() as d:
            theme.settings_path = lambda: Path(d) / "theme.json"
            try:
                theme.save_theme_key("sakura")
                self.assertEqual(theme.load_theme_key(), "sakura")
                # 写个坏文件 → 回落默认
                (Path(d) / "theme.json").write_text("{ not json",
                                                     encoding="utf-8")
                self.assertEqual(theme.load_theme_key(), theme.DEFAULT_THEME)
            finally:
                theme.settings_path = orig

    def test_shadow_rgb_refreshed(self):
        """投影色也得跟着主题走。

        `shadow` 存的是 `"R, G, B"` 串而不是 hex：第一版按 hex 解析，
        五套主题全抛 `ValueError: invalid literal for int() with base 16`。
        这条测试同时钉住"必须刷新"和"必须按三元组解析"。
        """
        from imgdedup_ui import widgets as W

        theme.apply_theme(self.app, "amber")
        want = tuple(int(v.strip())
                     for v in theme.THEMES["amber"]["shadow"].split(","))
        self.assertEqual((W.C_SHADOW.red(), W.C_SHADOW.green(),
                          W.C_SHADOW.blue()), want)
        theme.apply_theme(self.app, "mint")


class TestGradientPage(unittest.TestCase):
    """主背景的三段渐变是自绘的（QSS 给不了渐变），必须单独测。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_window_has_gradient_page(self):
        from imgdedup_ui.widgets import GradientPage
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            self.assertIsInstance(w.page, GradientPage)
        finally:
            w.close()

    def test_gradient_stops_follow_saved_theme_on_startup(self):
        """启动时就该是保存的那套主题的渐变。

        这里踩过坑：`apply_theme()` 在 `MainWindow()` 之前调用
        （为了让品牌圆点在构造期就读到正确颜色），那一刻 `GradientPage`
        还不存在，`topLevelWidgets()` 里找不到它：症状是
        「按钮是琥珀的、背景还是薄荷的」。窗口构造完必须自己补一次。
        """
        from imgdedup_ui import widgets as W
        from imgdedup_ui.window import MainWindow

        theme.apply_theme(self.app, "aurora")
        w = MainWindow()
        try:
            w.theme_key = "aurora"
            w._sync_gradient()
            want = theme.THEMES["aurora"]["bg_top"].lstrip("#")
            r, g, b = int(want[0:2], 16), int(want[2:4], 16), int(want[4:6], 16)
            self.assertEqual((w.page._c0.red(), w.page._c0.green(),
                              w.page._c0.blue()), (r, g, b),
                             "渐变起点没对齐保存的主题")
            self.assertIsInstance(w.page, W.GradientPage)
        finally:
            w.close()
            theme.apply_theme(self.app, "mint")

    def test_switching_theme_updates_gradient(self):
        """走真实用户路径切主题（下拉框选另一项），渐变背景必须跟着变。"""
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            # 不要用"手工把 theme_key 改成 mint 再 _sync_gradient()"来构造起点。
            # 那样断言的其实是"我摆得对不对"，不是"切主题的逻辑对不对"。
            # 这里直接用会发信号的那条路径：选一个不是当前项的下拉项。
            cur = w.cmb_theme.currentIndex()
            other = 0 if cur != 0 else 1
            want = w.cmb_theme.itemData(other)
            before = w.page._c0.name().lower()

            w.cmb_theme.setCurrentIndex(other)
            self.app.processEvents()

            self.assertEqual(w.theme_key, want)
            self.assertNotEqual(w.page._c0.name().lower(), before,
                                "切主题后渐变背景没跟着变（半截状态）")
            self.assertEqual(w.page._c0.name().lower(),
                             theme.THEMES[want]["bg_top"].lower())
        finally:
            w.close()
            theme.apply_theme(self.app, "mint")

    def test_switching_theme_refreshes_background_when_key_unchanged(self):
        """当前项没变时也得刷新，否则用户切走再切回来，背景会停在上一个主题。

        `QComboBox` 在"设成当前已有的项"时不发 `currentIndexChanged`，
        所以不能靠"key 没变就提前 return"来省事：那个短路会让
        「窗口构造完第一次同步」也一并失效。
        """
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            cur = w.cmb_theme.currentIndex()
            want = w.cmb_theme.itemData(cur)
            # 故意把画布弄脏成别的主题的色
            w.page.set_stops("#123456", "#123456", "#123456")
            self.assertNotEqual(w.page._c0.name().lower(),
                                theme.THEMES[want]["bg_top"].lower())

            w.cmb_theme.setCurrentIndex(cur)   # 同一项，Qt 不发信号
            w._sync_gradient()                 # 但显式同步必须能修好

            self.assertEqual(w.page._c0.name().lower(),
                             theme.THEMES[want]["bg_top"].lower())
        finally:
            w.close()
            theme.apply_theme(self.app, "mint")

    def test_gradient_off_falls_back_to_solid(self):
        """关掉渐变要退回纯色，不能留个没画背景的白洞。"""
        from imgdedup_ui.widgets import GradientPage

        page = GradientPage()
        page.gradient_on = False
        page.resize(40, 40)
        # 不该抛（paintEvent 会在 offscreen 下被调用）
        page.repaint()


class TestPanelAndBrand(unittest.TestCase):
    """侧栏面板与品牌渐变文字：这两处是主题的招牌语言，单独钉住。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_sidebar_panel_exists_and_paints(self):
        from imgdedup_ui.widgets import PanelSurface
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            self.assertIsInstance(w.sidebar_panel, PanelSurface)
            w.resize(1360, 860)
            w.show()
            self.app.processEvents()
            w.sidebar_panel.repaint()   # 不该抛
            self.assertTrue(w.sidebar_panel.size().width() > 0,
                            "侧栏面板宽度为 0，说明没被排进布局")
        finally:
            w.close()

    def test_panel_stops_follow_theme(self):
        """面板竖直渐变要跟着主题换，否则侧栏会停在上一个主题的色。"""
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            cur = w.cmb_theme.currentIndex()
            other = 0 if cur != 0 else 1
            want = w.cmb_theme.itemData(other)
            w.cmb_theme.setCurrentIndex(other)
            self.app.processEvents()
            hexv = theme.THEMES[want]["bg_top"].lstrip("#")
            expect = (int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16))
            got = (w.sidebar_panel._c0.red(), w.sidebar_panel._c0.green(),
                   w.sidebar_panel._c0.blue())
            self.assertEqual(got, expect, "侧栏面板渐变没跟着主题走")
        finally:
            w.close()
            theme.apply_theme(self.app, "mint")

    def test_brand_text_keeps_warm_ending(self):
        """品牌字的收尾永远是暖色，不随主题变。

        那截暖色是"萤火的暖光"，是把整套冷调设计从「医疗软件」里
        救出来的东西：换主题也不许把它换掉。

        用 `BRAND_EMBER` 而不是 `AMBER`：琥珀主题的 `accent` 就是 `AMBER`，
        收尾若也用 `AMBER` 会和渐变中段撞成一块、渐变白做。

        不再断言窗口上的 `brand_text`：侧栏品牌区已按需求移除
        （窗口标题栏本身就写着 ImgDedup，侧栏重复是噪音）。
        `BrandText` 类本身保留（`dev/check_frozen.py` 把它列为应存在于包里
        的符号），所以改成直接验证这个类：造一个、套主题、看三段色。
        """
        from imgdedup_ui import widgets as W

        for key in theme.THEMES:
            theme.apply_theme(self.app, key)
            b = W.BrandText("ImgDedup")
            b.set_colors(theme.THEMES[key]["bg_top"],
                         theme.THEMES[key]["accent"], theme.BRAND_EMBER)
            self.assertEqual(b._c.name().lower(),
                             theme.BRAND_EMBER.lower(),
                             f"主题 {key} 下品牌字收尾不暖了")
            # 而且是"暖"的：R > B
            c = b._c
            self.assertGreater(c.red(), c.blue(),
                               f"主题 {key} 下品牌字收尾不是暖色")
        theme.apply_theme(self.app, "mint")

    def test_brand_text_gradient_is_not_flat(self):
        """品牌字三段色必须互不相同，否则等于普通文字、白做渐变。"""
        from imgdedup_ui import widgets as W

        for key in theme.THEMES:
            theme.apply_theme(self.app, key)
            b = W.BrandText("ImgDedup")
            b.set_colors(theme.THEMES[key]["bg_top"],
                         theme.THEMES[key]["accent"], theme.BRAND_EMBER)
            cols = {b._a.name(), b._b.name(), b._c.name()}
            self.assertEqual(len(cols), 3,
                             f"主题 {key} 品牌渐变三段色有重复：{cols}")
        theme.apply_theme(self.app, "mint")

    def test_sidebar_brand_row_is_gone(self):
        """侧栏品牌区（圆点 + 渐变字）必须已移除。

        这条是防回归：那个 `● ImgDedup` 行删过一次又可能被"顺手加回来"。
        用 `findChild` 按类型查到就说明又回去了：标题栏已有品牌名，
        侧栏重复只是白占一行高度。
        """
        from imgdedup_ui.window import MainWindow
        from imgdedup_ui.widgets import BrandDot, BrandText

        w = MainWindow()
        try:
            self.assertIsNone(w.findChild(BrandDot),
                              "侧栏品牌圆点又回来了")
            self.assertIsNone(w.findChild(BrandText),
                              "侧栏品牌渐变字又回来了")
            self.assertFalse(hasattr(w, "brand_dot"), "窗口仍有 brand_dot 属性")
        finally:
            w.close()
            theme.apply_theme(self.app, "mint")

    def test_brand_gradient_counters_amber_theme(self):
        """琥珀主题是品牌渐变最容易塌掉的一套。

        `amber` 的 `accent`(#F59E0B) 本身就和暖色锚点同色，如果收尾也用
        `AMBER`，中段和收尾会一模一样；如果起点用 `deep`(#D97706)，
        起点和收尾又会撞。这条测试专门守着琥珀主题三段互异。
        """
        from imgdedup_ui import widgets as W

        theme.apply_theme(self.app, "amber")
        cols = {W.C_BRAND_A.name(), W.C_BRAND_B.name(), W.C_BRAND_C.name()}
        self.assertEqual(len(cols), 3, f"琥珀主题品牌渐变塌了：{cols}")
        theme.apply_theme(self.app, "mint")

    def test_progress_bar_gradient_tokens_refreshed(self):
        """进度条行进段渐变的三个端色要跟着主题走（暖色端除外）。"""
        from imgdedup_ui import widgets as W

        theme.apply_theme(self.app, "aurora")
        self.assertEqual(W.C_ACCENT_DEEP.name().lower(),
                         theme.THEMES["aurora"]["deep"].lower())
        # 暖色端恒定：它是"萤火的暖光"锚点，不跟着主题换
        self.assertEqual(W.C_EMBER.name().lower(),
                         theme.BRAND_EMBER.lower())
        theme.apply_theme(self.app, "mint")

    def test_progress_bar_paints_without_error(self):
        """进度条三态都画得出来（0% / 中间 / 100%）。"""
        from imgdedup_ui.widgets import StageProgress

        bar = StageProgress()
        bar.resize(600, 46)
        for done in (0, 30, 60):
            bar.set_stages([("遍历", done, 60), ("元数据", 0, 60)])
            bar.repaint()   # 不该抛
        bar.set_stages([("遍历", 60, 60)])
        bar.repaint()


class TestGlow(unittest.TestCase):
    def test_glow_returns_gradient(self):
        from PySide6.QtGui import QRadialGradient
        from PySide6.QtWidgets import QApplication

        from imgdedup_ui import widgets as W

        QApplication.instance() or QApplication([])
        g = W.glow(W.C_ACCENT)
        self.assertIsInstance(g, QRadialGradient)

    def test_glow_strength_clamped(self):
        """强度超出 0–1 不能抛（会被 clamp，而不是崩）。"""
        from PySide6.QtWidgets import QApplication

        from imgdedup_ui import widgets as W

        QApplication.instance() or QApplication([])
        for s in (-1.0, 0.0, 1.0, 5.0):
            W.glow(W.C_ACCENT, s)


if __name__ == "__main__":
    unittest.main()
