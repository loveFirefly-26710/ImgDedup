"""流萤主题：色值 token + QSS 生成 + 运行时可切换。

浅色主题：固定 hex 六件套打底，萤火光点做辨识度。
完整设计说明见 `docs/流萤主题美化设计方案.md`。

两条硬规则（写在这里，防止以后改崩）：

1. 裁决三态是语义色，不随主题变。 `KEEP` 永远 `#1D9E75`（通用安全绿），
   `DEL` / `UNDECIDED` 也固定。用户对"绿=安全、红=危险"的直觉不能为了好看让路。
2. 分类徽章四色也不随主题变。 那是分类标识，改了会让老用户失去肌肉记忆。

`widgets.py` 里放的是 `QColor` 常量，不读 QSS。切主题时必须同时刷新
那些常量并 `viewport().update()` 重绘，否则会出现「按钮变薄荷了、缩略图边框
还是蓝的」这种半截状态。`apply_theme()` 负责把这件事做完整。
"""

from __future__ import annotations

import json
from pathlib import Path

# ============================================================ 主题表
#: 五套预设。切主题只换 accent 三件套 + line/bg/text/muted/hover，
#: 语义色（keep/del/undecided）与徽章色一律不动。
THEMES: dict[str, dict[str, str]] = {
    # ---------------------------------------------------------------- 薄荷
    # 语义"萤火"：主背景是三段渐变（薄荷 → 近白 → 暖米）。
    # 那个暖米收尾是流萤的标志：萤火虫的光是暖的，纯冷绿会显得像医疗软件。
    # 三段色值：`#EAFAF6 → #F4FBF8 → #FDF8EF`。
    "mint": {
        "name": "流萤 · 薄荷",
        "accent": "#14B8A6", "deep": "#0D9488", "soft": "#DCF5EF",
        "line": "#D9ECE6", "bg": "#F6FBF9", "card": "#FFFFFF",
        "text": "#1D2B28", "muted": "#6F8A83", "hover": "#8FDCD0",
        #: 主按钮渐变的三个停靠点（浅 → 主 → 深）
        "grad_a": "#2DD4BF", "grad_b": "#14B8A6",
        #: 光晕颜色（RGB 三元组字符串，自绘时拼 rgba 用）
        "glow": "20, 184, 166",
        #: 主背景三段渐变的起止（自绘用，QSS 给不了渐变）
        "bg_top": "#EAFAF6", "bg_mid": "#F4FBF8", "bg_end": "#FDF8EF",
        #: 卡片柔和投影的颜色（rgba(23,108,96,0.06)）
        "shadow": "23, 108, 96",
    },
    # ---------------------------------------------------------------- 琥珀
    # 萤火虫的"暖"推到极致。背景收尾偏奶油黄，像夏夜灯火。
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
    # ---------------------------------------------------------------- 樱粉
    # 偏冷调的花色。背景收尾带一点香槟，避免整体发灰。
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
    # ---------------------------------------------------------------- 星穹
    # "夜"+ 萤火：背景收尾带一点点星光的冷白，不是纯蓝。
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
    # ---------------------------------------------------------------- 极光
    # 紫调，背景收尾偏淡青：对应极光"紫到青"的自然过渡。
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

# ------------------------------------------------------------ 语义色（恒定）
KEEP = "#1D9E75"
"""保留：永远不变。这是通用安全绿，改成薄荷色会跟「未裁决」混淆。"""

DEL = "#D94B4B"
"""待删：比原 #E24B4A 略沉一点，和 accent 同屏时更协调。"""

UNDECIDED = "#B4B2A9"
"""未裁决：中性暖灰，明确表达"还没决定"。"""

AMBER = "#F59E0B"
"""琥珀 —— 不随主题变。

它是「萤火虫的暖光」这个角色锚点：品牌渐变文字的暖色收尾、背景渐变的
暖米收尾都指向它。换主题时如果连它一起换掉，"流萤"就没了 —— 那截暖色
正是把整套冷调设计从"医疗软件"里救出来的东西。
"""

BRAND_EMBER = "#D97706"
"""品牌渐变文字的收尾色（比 `AMBER` 深一档）。

不能直接用 `AMBER`：琥珀主题的 `accent` 就是 `#F59E0B`，
   品牌渐变的中段和收尾会变成同一个色、渐变白做。
   深一档之后读起来才像"光晕的内芯"。
"""

PLACEHOLDER = "#EDF6F3"
"""缩略图未加载时的占位块（薄荷倾向的浅灰）。"""

#: 分类徽章（底, 字）—— 不随主题变
BADGE = {
    "exact": ("#E1F5EE", "#0F6E56"),
    "perceptual": ("#FAEEDA", "#854F0B"),
    "semantic": ("#E6F1FB", "#185FA5"),
    "cluster": ("#FCEBEB", "#A32D2D"),
}


def theme(key: str | None) -> dict[str, str]:
    """取主题色表；未知 key 回落到默认，绝不抛异常。"""
    return THEMES.get(key or DEFAULT_THEME, THEMES[DEFAULT_THEME])


def build_qss(key: str | None = None) -> str:
    """按主题生成 QSS。

    QSS 不支持 `box-shadow`：萤火光点与卡片投影只能自绘
    （见 `widgets.py` 里的 `glow()` 和 `_paint_page_bg()`），不要在这里写阴影。

    QSS 支持 `qlineargradient`，但语法与 CSS 不同（是函数式坐标写法）。
    主按钮那点"流萤"的层次感就靠它：纯色按钮会显得很平。
    """
    t = theme(key)
    return f"""
QWidget {{ font-size: 13px; color: {t['text']}; }}
QMainWindow, QWidget#page {{ background: {t['bg']}; }}
QFrame#toolbar, QFrame#footer {{ background: {t['bg']}; }}
QFrame#toolbar {{ border-bottom: 1px solid {t['line']}; }}
QFrame#footer  {{ border-top: 1px solid {t['line']}; }}
/* 这两条必须限定在 QListWidget 上，不能写成裸 `QListView`。
   `QComboBox` 的弹出列表 view 是 `QListView` 的子类，裸 `QListView`
   会把"背景透明 + 无边框"也套到弹出列表上：于是下面辛苦写的
   `QComboBox QAbstractItemView` 规则虽然命中了，但弹出的那块底是
   透明的：实测离屏采样三行全是 `#ffffff`（窗口底色透出来），
   选中行和未选中行完全看不出区别，用户依旧会把它读成"空选项"。 */
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
 * `QComboBox` 的弹出列表不是 `QListWidget` 的样式能覆盖的 ——
 * 它挂在 `QComboBox QAbstractItemView` 上。早先只写了 `QListView::item`，
 * 而 `QComboBox` 的 view 是 `QListView` 的子类，选择器匹配不到，
 * 于是弹出列表用的是系统默认外观：选中那一行是一条和背景色差极小的浅灰，
 * 看起来就像"列表里有个空白选项"（用户截图反馈的正是这个）。
 * 所以下面必须显式写 `QComboBox QAbstractItemView`。
 */
QComboBox {{ padding: 4px 10px; }}
QComboBox:hover {{ border-color: {t['hover']}; }}
QComboBox:focus {{ border-color: {t['accent']}; }}
QComboBox:disabled {{ color: {t['muted']}; background: {t['bg']}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
                        width: 22px; border: none; background: transparent; }}
/* 把 Qt 自带的箭头换成一套自绘的：默认那个在浅色主题下是深灰实心块，
   和圆角浅色框放在一起显得很重。用 border 拼一个 V 形，
   这样它天然继承 `color`，:hover / :focus 时跟着变主题色。 */
QComboBox::down-arrow {{ image: none; width: 8px; height: 8px;
                         border-left: 1.5px solid {t['muted']};
                         border-bottom: 1.5px solid {t['muted']};
                         margin-right: 9px; }}
QComboBox:hover::down-arrow {{ border-color: {t['deep']}; }}
QComboBox:focus::down-arrow {{ border-color: {t['accent']}; }}

/* ---- 下拉框的弹出列表 ----
   这里只管"容器"的几何与底色；行内的选中/悬停底色不在这里。
   原因见 `apply_theme()` 第 4 步的长注释：只要 QComboBox 控件本身命中 QSS，
   popup 的选中行就不采纳 `::item:selected {{ background }}`，也不采纳
   palette.Highlight：实测各种写法都不行。所以行的底色交给
   `widgets.ComboItemDelegate` 自绘，这里只保留不影响落地的几何属性。 */
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


# ============================================================ 持久化
def settings_path() -> Path:
    """主题偏好落盘位置（和缓存同目录，都在 LOCALAPPDATA）。"""
    import os

    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(base) / "ImgDedup"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d / "theme.json"


def load_theme_key() -> str:
    """读上次选的主题；任何异常都回落默认值：配置坏了不该让软件起不来。"""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        key = data.get("theme")
        if key in THEMES:
            return key
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_THEME


def save_theme_key(key: str) -> None:
    try:
        settings_path().write_text(
            json.dumps({"theme": key}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def apply_theme(app, key: str) -> None:
    """把主题套到整个应用，并刷新自绘控件的配色常量。

    顺序很重要：先换常量、再换 QSS。因为 `widgets.py` 的 `QColor`
    常量是世界可变的，视图重绘时读的是它们；先换 QSS 而没换常量，会出现
    「按钮变薄荷了、缩略图边框还是蓝的」的半截状态。
    """
    from . import widgets as W

    t = theme(key)

    # 1) 自绘控件读的 QColor 常量
    #
    # 必须就地改这些 QColor 对象，不能重新赋值成新对象。
    # 别处（delegate / BrandDot / StageProgress）已经持有它们的引用
    # （有些还是模块级默认参数），换对象等于换了个人，旧的那份永远不变。
    #
    # 用 `setRgb` 而不是 `setNamedColor`：后者在 PySide6 6.11 已废弃。
    def _set(color, hexval, alpha=None):
        r, g, b, _a = _rgba(hexval, 255 if alpha is None else alpha)
        color.setRgb(r, g, b) if alpha is None else color.setRgb(r, g, b, alpha)

    _set(W.C_BORDER, t["line"])
    _set(W.C_TEXT, t["text"])
    _set(W.C_TEXT_DIM, t["muted"])
    _set(W.C_PLACEHOLDER, PLACEHOLDER)
    # 选中环刻意用 deep 而不是 accent：#14B8A6 明度偏高，叠在花了呼哨的
    # 图片上会被吃掉；深一档才压得住。
    _set(W.C_FOCUS, t["deep"])
    _set(W.C_HOVER, t["hover"])
    _set(W.C_CARD_FOCUS, t["soft"])
    _set(W.C_FOCUS_WASH, t["deep"], 26)
    _set(W.C_KEEP, KEEP)
    _set(W.C_DEL, DEL)
    _set(W.C_UNDECIDED, UNDECIDED)
    #: 卡片投影色跟着主题走。琥珀主题下用薄荷绿的投影会"发脏"，
    #: 所以每套主题都各自给了 shadow（见 THEMES 表）。
    #:
    #: 注意 `shadow` 存的是 `"R, G, B"` 三元组字符串、不是 hex。
    #: 它只用于自绘时拼 `rgba()`，所以不能走 `_set()`。这里单独解析。
    _sr, _sg, _sb = (int(v.strip()) for v in t["shadow"].split(","))
    W.C_SHADOW.setRgb(_sr, _sg, _sb)
    W.C_PANEL_SHADOW.setRgb(_sr, _sg, _sb)
    #: 面板渐变：由背景三段色派生（上 = 略深一点的 bg_top，下 = 更暖的收尾）。
    #: 比主背景深一档，面板才"浮"得起来而不是糊进背景里。
    _set(W.C_PANEL_TOP, t["bg_top"])
    _set(W.C_PANEL_MID, t["bg_mid"])
    _set(W.C_PANEL_END, t["bg_end"])
    _set(W.C_PANEL_LINE, t["line"])
    #: 品牌渐变文字：`accent-deep → accent → amber`。
    #: 那截暖色收尾 不随主题变（它是"萤火的暖光"，是角色锚点）——
    #: 保证"不管换哪套主题，品牌字收尾永远是暖的"。
    #:
    #: 两处不能想当然：
    #:   · 起点不能直接用 `deep`：琥珀主题的 `deep`(#D97706) 本身就是暖色，
    #:     会和收尾撞成一片、渐变看不出来。起点统一取主背景的冷色端
    #:     （`bg_top`）——它永远和暖色分得开，"冷→暖"这个叙事在任何主题下成立。
    #:   · 收尾不能用 `AMBER`：琥珀主题的 `accent` 就是 `#F59E0B`，
    #:     中段和收尾会一模一样。所以收尾统一再深一档（`#D97706`），
    #:     读起来才像"光晕的内芯"，而不是同一块平色铺两遍。
    _set(W.C_BRAND_A, t["bg_top"])
    _set(W.C_BRAND_B, t["accent"])
    W.C_BRAND_C.setRgb(*_rgba(BRAND_EMBER, 255)[:3])
    #: 进度条行进段的渐变三端：深主色 → 主色 → 暖琥珀。
    #: `C_EMBER` 和品牌字收尾共用同一个暖色：全界面只有一处暖色锚点，
    #: 才不会显得花。
    _set(W.C_ACCENT_DEEP, t["deep"])
    W.C_EMBER.setRgb(*_rgba(BRAND_EMBER, 255)[:3])
    #: 即时提示气泡：跟卡片同一套底色/描边/文字。
    #: 它是常驻浮层，配色必须和主界面同源，否则看起来像外挂的。
    _set(W.C_TIP_BG, t["card"])
    _set(W.C_TIP_BORDER, t["line"])
    _set(W.C_TIP_TEXT, t["text"])
    for k, (bg, fg) in BADGE.items():
        if k in W.BADGE:
            _set(W.BADGE[k][0], bg)
            _set(W.BADGE[k][1], fg)
    W.set_accent(t["accent"])

    # 2) 主背景的渐变页（自绘，QSS 管不到）
    #
    # 这里拿不到窗口引用（`apply_theme` 只收到 `app`），所以从
    # `topLevelWidgets()` 里找。找到几个就刷几个：正常只有主窗口一个，
    # 但测试里可能同时存在多个窗口，逐个刷才不会漏。
    # 找不到也不要紧：新建的窗口会在 `_build_ui()` 后自己调一次。
    for w in app.topLevelWidgets():
        page = getattr(w, "page", None)
        if isinstance(page, W.GradientPage):
            page.set_stops(t["bg_top"], t["bg_mid"], t["bg_end"])
            page.update()
        # 侧栏面板和品牌字也走同一条反查路径：它们是窗口的子控件，
        # 从 `app` 这头够不着，只能顺着窗口往下摸。
        sync = getattr(w, "_sync_gradient", None)
        if callable(sync):
            sync()

    # 3) 全局样式表
    app.setStyleSheet(build_qss(key))

    # 4) 下拉框弹出列表的"选中行底色"必须由 delegate 自绘，QSS 上不了色
    #
    # 这是 Qt 样式表一个既难发现又绕不过去的坑：只要 `QComboBox` 控件本身
    # 命中任意 QSS（本项目有 `QLineEdit, QComboBox {{ padding...; border...; }}`），
    # 弹出列表的选中行就不再采纳 `::item:selected {{ background }}`，
    # 也不再采纳 view 的 `palette.Highlight`。离屏逐像素实测（取行中部底色）：
    #   · 只写 `::item:selected {{ background: soft }}` → 仍是 Qt 默认灰描边 + 白底
    #   · 再叠一条裸 `QWidget {{ font-size... }}`          → 整行全白
    #   · 设 view 的 `palette.Highlight = soft`           → palette 写了但画面不变
    #   · `selection-background-color` / `:active` 等变体  → 全部无效
    #   · 改用自绘 delegate（`widgets.ComboItemDelegate`） → 稳定生效
    # 所以选中行交给 delegate 画，QSS 里那几条只管几何（行高/内边距/圆角）。
    _apply_combo_style(app, t)


def _apply_combo_style(app, t: dict) -> None:
    """给所有 `QComboBox` 装上自绘行样式的 delegate，并刷新它的配色常量。

    每次换主题都要重刷 `_DELEGATE_*`：它们和 `widgets.py` 里其它配色一样，
    是世界可变的 `QColor`（delegate 重绘时现读），所以就地 `setRgb` 即可，
    不能换对象。
    """
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

    # `app.findChildren()` 找不到无父的顶层控件（它们不是 app 的
    # QObject 子对象），实测返回 0 个。所以必须同时扫 `topLevelWidgets()`
    #：主窗口里的对话框、以及测试里裸建的 QComboBox 都靠这一路兜住。
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
