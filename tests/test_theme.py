"""`imgdedup_ui/theme.py`：主题 token、QSS 生成与切换。

`apply_theme()` 的四步顺序是这里最容易踩的地方，用例重点在「常量是就地刷新
而不是重新赋值」—— 重新赋值会让已经持有引用的自绘控件永远停在旧颜色上。
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _qt

from imgdedup_ui import theme as T
from imgdedup_ui import widgets as W

TOKENS = {
    "name", "accent", "deep", "soft", "line", "bg", "card", "text", "muted",
    "hover", "grad_a", "grad_b", "glow", "bg_top", "bg_mid", "bg_end", "shadow",
}


class TestThemes(unittest.TestCase):

    def test_five_presets(self):
        self.assertEqual(len(T.THEMES), 5)

    def test_every_preset_defines_the_same_tokens(self):
        for key, t in T.THEMES.items():
            with self.subTest(theme=key):
                self.assertEqual(set(t), TOKENS)

    def test_hex_values_are_well_formed(self):
        for key, t in T.THEMES.items():
            for name, val in t.items():
                if name in ("name", "glow", "shadow"):
                    continue
                with self.subTest(theme=key, token=name):
                    self.assertRegex(val, r"^#[0-9A-Fa-f]{6}$")

    def test_glow_and_shadow_are_rgb_triples_not_hex(self):
        """自绘时要拼进 `rgba()`，所以这两个是 `"R, G, B"` 字符串。

        按 hex 去解析会抛 `ValueError: invalid literal for int() with base 16: ', '`。
        """
        for key, t in T.THEMES.items():
            for name in ("glow", "shadow"):
                parts = [p.strip() for p in t[name].split(",")]
                with self.subTest(theme=key, token=name):
                    self.assertEqual(len(parts), 3)
                    for p in parts:
                        self.assertTrue(0 <= int(p) <= 255)

    def test_theme_lookup_falls_back_to_default(self):
        self.assertIs(T.theme("mint"), T.THEMES["mint"])
        self.assertIs(T.theme("no-such-theme"), T.THEMES[T.DEFAULT_THEME])
        self.assertIs(T.theme(None), T.THEMES[T.DEFAULT_THEME])

    def test_default_theme_exists(self):
        self.assertIn(T.DEFAULT_THEME, T.THEMES)


class TestSemanticColours(unittest.TestCase):

    def test_decision_colours_are_module_constants_not_per_theme(self):
        """三态色跨主题恒定 —— 用户对「绿 = 安全、红 = 危险」的直觉不能为了好看让路。"""
        for name in ("KEEP", "DEL", "UNDECIDED", "BRAND_EMBER", "PLACEHOLDER"):
            with self.subTest(name=name):
                self.assertRegex(getattr(T, name), r"^#[0-9A-Fa-f]{6}$")
        for t in T.THEMES.values():
            self.assertNotIn("keep", t)
            self.assertNotIn("del", t)

    def test_badge_table_covers_the_four_kinds(self):
        self.assertEqual(set(T.BADGE), {"exact", "perceptual", "semantic", "cluster"})
        for bg, fg in T.BADGE.values():
            self.assertRegex(bg, r"^#[0-9A-Fa-f]{6}$")
            self.assertRegex(fg, r"^#[0-9A-Fa-f]{6}$")

    def test_badge_is_identical_across_themes(self):
        # 徽章色不在 THEMES 里，所以天然不随主题变
        for t in T.THEMES.values():
            self.assertNotIn("badge", t)


class TestBuildQss(unittest.TestCase):

    def test_contains_the_theme_text_colour(self):
        qss = T.build_qss("mint")
        self.assertIn(T.THEMES["mint"]["text"], qss)

    def test_switching_theme_switches_colours(self):
        self.assertNotEqual(T.build_qss("mint"), T.build_qss("amber"))

    def test_no_bare_qlistview_selector(self):
        """裸 `QListView` 会把「透明背景 + 无边框」也套到 QComboBox 的弹出列表上。

        那样弹出的底是透明的，选中行和未选中行看不出区别，用户会读成「空选项」。

        断言前必须先剥掉 CSS 注释 —— QSS 里那段注释本身就在解释这件事，
        里面明明白白写着 `QListView`，不剥掉会一直误报。
        """
        for key in T.THEMES:
            qss = re.sub(r"/\*.*?\*/", "", T.build_qss(key), flags=re.S)
            selectors = [ln.split("{")[0].strip()
                         for ln in qss.splitlines() if "{" in ln]
            with self.subTest(theme=key):
                for sel in selectors:
                    self.assertNotIn("QListView", sel, f"裸 QListView 选择器：{sel}")
                self.assertTrue(any("QListWidget" in s for s in selectors),
                                "QListWidget 规则不见了")

    def test_unknown_key_falls_back(self):
        self.assertEqual(T.build_qss("nope"), T.build_qss(T.DEFAULT_THEME))


class TestSettingsPath(unittest.TestCase):
    """把 `settings_path()` 指到临时目录，别去动用户真实的 theme.json。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imgdedup_theme_")
        self.path = Path(self.tmp.name) / "theme.json"
        patcher = mock.patch.object(T, "settings_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        T.save_theme_key("amber")
        self.assertEqual(T.load_theme_key(), "amber")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "amber")

    def test_unknown_saved_key_falls_back(self):
        self.path.write_text('{"theme": "definitely-not-a-theme"}',
                             encoding="utf-8")
        self.assertEqual(T.load_theme_key(), T.DEFAULT_THEME)

    def test_missing_file_falls_back(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(T.load_theme_key(), T.DEFAULT_THEME)


class TestApplyTheme(unittest.TestCase):

    def setUp(self):
        self.app = _qt.app()
        self.saved = T.load_theme_key()

    def tearDown(self):
        T.apply_theme(self.app, self.saved)

    def test_refreshes_widgets_constants_in_place(self):
        """常量必须是就地 `setRgb`。

        自绘控件在 `__init__` 里就把 `C_TEXT` 这类对象存下来了，重新赋值
        `W.C_TEXT = QColor(...)` 只会换掉模块属性，控件手里还是旧对象。
        """
        ids_before = {n: id(getattr(W, n)) for n in ("C_TEXT", "C_BORDER", "C_KEEP")}
        colors_before = {n: getattr(W, n).name() for n in ids_before}

        other = "amber" if T.load_theme_key() != "amber" else "mint"
        T.apply_theme(self.app, other)

        for n, obj_id in ids_before.items():
            with self.subTest(const=n):
                self.assertEqual(id(getattr(W, n)), obj_id, "常量被重新赋值了")
        self.assertNotEqual({n: getattr(W, n).name() for n in ids_before},
                            colors_before)

    def test_semantic_colours_survive_a_theme_switch(self):
        T.apply_theme(self.app, "mint")
        keep = W.C_KEEP.name()
        del_ = W.C_DEL.name()
        T.apply_theme(self.app, "amber")
        self.assertEqual(W.C_KEEP.name(), keep)
        self.assertEqual(W.C_DEL.name(), del_)

    def test_shadow_rgb_is_parsed(self):
        T.apply_theme(self.app, "mint")
        t = T.THEMES["mint"]
        r, g, b = (int(v.strip()) for v in t["shadow"].split(","))
        self.assertEqual((W.C_SHADOW.red(), W.C_SHADOW.green(), W.C_SHADOW.blue()),
                         (r, g, b))

    def test_stylesheet_is_installed(self):
        T.apply_theme(self.app, "mint")
        self.assertIn(T.THEMES["mint"]["text"], self.app.styleSheet())

    def test_badge_colours_are_refreshed(self):
        T.apply_theme(self.app, "mint")
        for kind, (bg, _fg) in T.BADGE.items():
            with self.subTest(kind=kind):
                self.assertEqual(W.BADGE[kind][0].name().upper(), bg.upper())


if __name__ == "__main__":
    unittest.main()
