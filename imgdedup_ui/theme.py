"""主题 token 与 QSS。

五套主题共用同一组 token 名，`build_qss()` 把它们填进样式表。
`glow` / `shadow` 是 "R, G, B" 字符串（要拼进 rgba()），不是十六进制色值，别混。"""

from __future__ import annotations

import json
from pathlib import Path

THEMES: dict[str, dict[str, str]] = {
    "mint": {
        "name": "流萤 · 薄荷",
        "accent": "#14B8A6", "deep": "#0D9488", "soft": "#DCF5EF",
        "line": "#D9ECE6", "bg": "#F6FBF9", "card": "#FFFFFF",
        "text": "#1D2B28", "muted": "#6F8A83", "hover": "#8FDCD0",
        "grad_a": "#2DD4BF", "grad_b": "#14B8A6",
        "glow": "20, 184, 166",
        "bg_top": "#EAFAF6", "bg_mid": "#F4FBF8", "bg_end": "#FDF8EF",
        "shadow": "23, 108, 96",
    },
    "amber": {
        "name": "琥珀 · 余烬",
        "accent": "#F59E0B", "deep": "#D97706", "soft": "#FEF3E2",
        "line": "#F1E2C8", "bg": "#FDFAF4", "card": "#FFFFFF",
        "text": "#2B2318", "muted": "#8A7A63", "hover": "#EFC583",
        "grad_a": "#FBBF24", "grad_b": "#F59E0B",
        "glow": "245, 158, 11",
        "bg_top": "#FEF6E7", "bg_mid": "#FEFBF4", "bg_end": "#FDF3E0",
        "shadow": "140, 95, 20",
    },
    "sakura": {
        "name": "樱粉 · 梦境",
        "accent": "#EC7FA9", "deep": "#DB2777", "soft": "#FCE7F3",
        "line": "#F2DCE7", "bg": "#FDF8FA", "card": "#FFFFFF",
        "text": "#2B1D24", "muted": "#8A6F7C", "hover": "#F3B3CB",
        "grad_a": "#F4A0C0", "grad_b": "#EC7FA9",
        "glow": "236, 127, 169",
        "bg_top": "#FDF0F5", "bg_mid": "#FEF8FB", "bg_end": "#FDF4EC",
        "shadow": "150, 70, 105",
    },
    "starlight": {
        "name": "星穹 · 蓝",
        "accent": "#5B8DEF", "deep": "#3B6FD4", "soft": "#E3EDFC",
        "line": "#D6E3F5", "bg": "#F7FAFE", "card": "#FFFFFF",
        "text": "#1B2330", "muted": "#6F7E93", "hover": "#9DBBF4",
        "grad_a": "#7BA5F5", "grad_b": "#5B8DEF",
        "glow": "91, 141, 239",
        "bg_top": "#EEF5FE", "bg_mid": "#F7FAFE", "bg_end": "#F6F4FD",
        "shadow": "40, 80, 150",
    },
    "aurora": {
        "name": "极光 · 紫",
        "accent": "#8B7CF6", "deep": "#6D5CE7", "soft": "#EDE9FE",
        "line": "#E0DBF6", "bg": "#FAF9FE", "card": "#FFFFFF",
        "text": "#221E33", "muted": "#78708F", "hover": "#B6ACF9",
        "grad_a": "#A79BFA", "grad_b": "#8B7CF6",
        "glow": "139, 124, 246",
        "bg_top": "#F3F0FE", "bg_mid": "#FAF9FE", "bg_end": "#EEF6FB",
        "shadow": "80, 70, 150",
    },
}

DEFAULT_THEME = "mint"

KEEP = "#1D9E75"

DEL = "#D94B4B"

UNDECIDED = "#B4B2A9"

BRAND_EMBER = "#D97706"

PLACEHOLDER = "#EDF6F3"

BADGE = {
    "exact": ("#E1F5EE", "#0F6E56"),
    "perceptual": ("#FAEEDA", "#854F0B"),
    "semantic": ("#E6F1FB", "#185FA5"),
    "cluster": ("#FCEBEB", "#A32D2D"),
}


def theme(key: str | None) -> dict[str, str]:
    return THEMES.get(key or DEFAULT_THEME, THEMES[DEFAULT_THEME])


def build_qss(key: str | None = None) -> str:
    t = theme(key)
    return f"""
QWidget {{ font-size: 13px; color: {t['text']}; }}
QMainWindow, QWidget#page {{ background: {t['bg']}; }}
QFrame#toolbar, QFrame#footer {{ background: {t['bg']}; }}
QFrame#toolbar {{ border-bottom: 1px solid {t['line']}; }}
QFrame#footer  {{ border-top: 1px solid {t['line']}; }}
/* 这两条必须限定在 QListWidget 上，不能写成裸 `QListView`。
   `QComboBox` 的弹出列表 view 是 `QListView` 的子类，裸规则会把
   "背景透明 + 无边框"也套到弹出列表上。那样下面写的
   `QComboBox QAbstractItemView` 规则虽然命中了，弹出的底却是透明的
   （离屏采样三行都是 `#ffffff`，窗口底色透出来），选中行和未选中行
   看不出区别，用户会把它读成"空选项"。 */
QListWidget {{ background: transparent; border: none; }}
QListWidget::item {{ border: none; }}
QPushButton {{ padding: 5px 12px; border: 1px solid {t['line']};
              border-radius: 8px; background: {t['card']}; }}
QPushButton:hover {{ border-color: {t['hover']}; color: {t['deep']}; }}
QPushButton:focus {{ border-color: {t['accent']}; }}
QPushButton:disabled {{ color: {t['muted']}; border-color: {t['line']};
                       background: {t['bg']}; }}
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 {t['grad_a']}, stop:0.55 {t['grad_b']},
                stop:1 {t['deep']});
    color: #FFFFFF; border: 1px solid transparent; font-weight: 600;
    border-radius: 9px; padding: 6px 14px; }}
QPushButton#primary:hover {{ border-color: {t['deep']}; }}
QPushButton#primary:pressed {{ background: {t['deep']}; }}
QPushButton#primary:disabled {{ background: {t['soft']}; color: {t['muted']};
                               border-color: {t['soft']}; }}
QPushButton#danger {{ color: {DEL}; border: 1px solid {t['line']}; }}
QPushButton#danger:hover {{ border-color: {DEL}; color: {DEL}; }}
QLineEdit, QComboBox {{ padding: 4px 8px; border: 1px solid {t['line']};
                        border-radius: 8px; background: {t['card']}; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {t['accent']};
                                    background: {t['card']}; }}
QLineEdit:read-only {{ background: {t['bg']}; }}

/* ---------------------------------------------------------------- 下拉框
 *
 * `QComboBox` 的弹出列表不受 `QListWidget` 样式影响，它挂在
 * `QComboBox QAbstractItemView` 上。只写 `QListView::item` 时选择器
 * 匹配不到（`QComboBox` 的 view 是 `QListView` 的子类），弹出列表会
 * 退回系统默认外观：选中行是一条和背景色差极小的浅灰，看起来像
 * "列表里有个空白选项"。所以下面必须显式写 `QComboBox QAbstractItemView`。
 */
QComboBox {{ padding: 4px 10px; }}
QComboBox:hover {{ border-color: {t['hover']}; }}
QComboBox:focus {{ border-color: {t['accent']}; }}
QComboBox:disabled {{ color: {t['muted']}; background: {t['bg']}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
                        width: 22px; border: none; background: transparent; }}
/* 把 Qt 自带的箭头换成自绘的。默认那个在浅色主题下是深灰实心块，
   和圆角浅色框放一起显得很重。用 border 拼一个 V 形，它会继承
   `color`，:hover / :focus 时跟着变主题色。 */
QComboBox::down-arrow {{ image: none; width: 8px; height: 8px;
                         border-left: 1.5px solid {t['muted']};
                         border-bottom: 1.5px solid {t['muted']};
                         margin-right: 9px; }}
QComboBox:hover::down-arrow {{ border-color: {t['deep']}; }}
QComboBox:focus::down-arrow {{ border-color: {t['accent']}; }}

/* ---- 下拉框的弹出列表 ----
   这里只管"容器"的几何与底色，行内的选中/悬停底色不在这里。
   原因见 `apply_theme()` 第 4 步的长注释：只要 QComboBox 控件本身命中 QSS，
   popup 的选中行就不采纳 `::item:selected {{ background }}`，也不采纳
   palette.Highlight。所以行的底色交给 `widgets.ComboItemDelegate` 自绘，
   这里只留不影响落地的几何属性。 */
QComboBox QAbstractItemView {{
    background: {t['card']};
    border: 1px solid {t['line']};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}
/* 行高统一：delegate 按 rect 画的圆角块才不会一行高一行矮 */
QComboBox QAbstractItemView::item {{
    min-height: 26px;
    color: {t['text']};
}}
QListWidget {{ background: transparent; }}
QListWidget::item {{ border-radius: 6px; padding: 3px 4px; }}
QListWidget::item:selected {{ background: {t['soft']};
                             color: {t['deep']}; }}
QListWidget::item:hover {{ background: {t['bg']}; }}
QCheckBox {{ padding: 2px 0; }}
QSplitter::handle {{ background: {t['line']}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{ background: {t['hover']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['line']}; border-radius: 5px;
                              min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {t['hover']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMenu {{ background: {t['card']}; border: 1px solid {t['line']};
        border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t['soft']}; color: {t['deep']}; }}
QMenu::item:disabled {{ color: {t['muted']}; }}
QMenu::separator {{ height: 1px; background: {t['line']}; margin: 4px 8px; }}
QToolTip {{ background: {t['card']}; color: {t['text']};
           border: 1px solid {t['line']}; padding: 4px 6px; }}
QSlider::groove:horizontal {{ height: 3px; background: {t['line']};
                             border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {t['accent']}; width: 12px;
                             margin: -5px 0; border-radius: 6px; }}
QSlider::sub-page:horizontal {{ background: {t['accent']};
                               border-radius: 2px; }}
QLabel#sectionLabel {{ color: {t['muted']}; font-weight: 600;
                      letter-spacing: 0.4px; }}
QLabel#modeHint {{ color: {t['muted']}; padding-left: 4px; }}
"""


def settings_path() -> Path:
    import os

    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(base) / "ImgDedup"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "theme.json"


def load_theme_key() -> str:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        key = data.get("theme")
        if key in THEMES:
            return key
    except Exception:
        pass
    return DEFAULT_THEME


def save_theme_key(key: str) -> None:
    try:
        settings_path().write_text(
            json.dumps({"theme": key}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass


def apply_theme(app, key: str) -> None:
    """切换主题。四步，顺序不能换：

    1. 就地刷新 `widgets` 的 `C_*` 常量 —— 自绘控件 paint 时直接读它们
    2. 通知已经建出来的自绘控件（渐变页底色、主窗口的渐变同步）
    3. 换掉全局 QSS
    4. 给每个 QComboBox 装自绘 delegate

    先刷常量、再重绘：反过来的话控件会按旧颜色画一帧，看起来像闪一下。
    QSS 选择器碰不到下拉项，所以第 4 步必须单独做。
    """
    from . import widgets as W

    t = theme(key)

    def _set(color, hexval, alpha=None):
        r, g, b, _a = _rgba(hexval, 255 if alpha is None else alpha)
        color.setRgb(r, g, b) if alpha is None else color.setRgb(r, g, b, alpha)

    _set(W.C_BORDER, t["line"])
    _set(W.C_TEXT, t["text"])
    _set(W.C_TEXT_DIM, t["muted"])
    _set(W.C_PLACEHOLDER, PLACEHOLDER)
    _set(W.C_FOCUS, t["deep"])
    _set(W.C_HOVER, t["hover"])
    _set(W.C_CARD_FOCUS, t["soft"])
    _set(W.C_FOCUS_WASH, t["deep"], 26)
    _set(W.C_KEEP, KEEP)
    _set(W.C_DEL, DEL)
    _set(W.C_UNDECIDED, UNDECIDED)
    _sr, _sg, _sb = (int(v.strip()) for v in t["shadow"].split(","))
    W.C_SHADOW.setRgb(_sr, _sg, _sb)
    W.C_PANEL_SHADOW.setRgb(_sr, _sg, _sb)
    _set(W.C_PANEL_TOP, t["bg_top"])
    _set(W.C_PANEL_MID, t["bg_mid"])
    _set(W.C_PANEL_END, t["bg_end"])
    _set(W.C_PANEL_LINE, t["line"])
    _set(W.C_BRAND_A, t["bg_top"])
    _set(W.C_BRAND_B, t["accent"])
    W.C_BRAND_C.setRgb(*_rgba(BRAND_EMBER, 255)[:3])
    _set(W.C_ACCENT_DEEP, t["deep"])
    W.C_EMBER.setRgb(*_rgba(BRAND_EMBER, 255)[:3])
    _set(W.C_TIP_BG, t["card"])
    _set(W.C_TIP_BORDER, t["line"])
    _set(W.C_TIP_TEXT, t["text"])
    for k, (bg, fg) in BADGE.items():
        if k in W.BADGE:
            _set(W.BADGE[k][0], bg)
            _set(W.BADGE[k][1], fg)
    W.set_accent(t["accent"])

    # 已经建出来的自绘控件得手动通知：它们不读 QSS，只在 paint 时取常量
    for w in app.topLevelWidgets():
        page = getattr(w, "page", None)
        if isinstance(page, W.GradientPage):
            page.set_stops(t["bg_top"], t["bg_mid"], t["bg_end"])
            page.update()
        sync = getattr(w, "_sync_gradient", None)
        if callable(sync):
            sync()

    app.setStyleSheet(build_qss(key))

    # QComboBox 的下拉项是自绘的，QSS 管不到，只能逐个装 delegate
    _apply_combo_style(app, t)


def _apply_combo_style(app, t: dict) -> None:
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QComboBox

    from . import widgets as W

    def _c(color, hexval):
        q = QColor(hexval)
        color.setRgb(q.red(), q.green(), q.blue())

    _c(W._DELEGATE_SEL_BG, t["soft"])
    _c(W._DELEGATE_SEL_TEXT, t["deep"])
    _c(W._DELEGATE_SEL_LINE, t["accent"])
    _c(W._DELEGATE_HOVER_BG, t["bg"])
    _c(W._DELEGATE_TEXT, t["text"])

    targets = list(app.findChildren(QComboBox))
    for w in app.topLevelWidgets():
        if isinstance(w, QComboBox):
            targets.append(w)
        targets.extend(w.findChildren(QComboBox))

    seen = set()
    for cb in targets:
        if id(cb) in seen:
            continue
        seen.add(id(cb))
        cb.setItemDelegate(W.ComboItemDelegate(cb))


def _rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
