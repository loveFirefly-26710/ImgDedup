"""自绘控件：分组卡片 delegate + 多段进度条。

delegate 只画可见行（Qt 的 model/view 天然如此），所以图库规模不影响开销。
`paint()` 里绝不加载图片：未命中缓存就画占位色块并异步请求。
"""

from __future__ import annotations

import hashlib
import math
import itertools
import time
from typing import Optional

from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt,
                            QThread, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QGradient,
                           QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QLabel, QSizePolicy, QStyle,
                               QStyledItemDelegate, QWidget)

from .model import DELETE, THUMB_SIZE, GroupModel, ThumbCache

# ---- 卡片尺寸常量（改这里就能调整密度）----
CARD_PAD = 12
LABEL_H = 22
ITEM_GAP = 10
CAPTION_H = 16
THUMB_GAP = 6
MIN_CARD_H = CARD_PAD * 2 + LABEL_H + 6 + THUMB_SIZE + CAPTION_H

# ---- 配色（浅色主题，与设计稿一致）----
#
# 这些是世界可变的 QColor 对象，不读 QSS。切主题时由
# `theme.apply_theme()` 就地 `setNamedColor()` 改值，所以：
#   · 别把它们当 `dict` 的 key（可变对象哈希会变）
#   · 别在别处缓存它们的副本，否则切主题后拿到的是旧色
C_CARD = QColor("#FFFFFF")
C_BORDER = QColor("#E3E3E0")
C_TEXT = QColor("#2C2C2A")
C_TEXT_DIM = QColor("#888780")
C_KEEP = QColor("#1D9E75")
C_DEL = QColor("#E24B4A")
C_UNDECIDED = QColor("#D3D1C7")
C_PLACEHOLDER = QColor("#F1EFE8")

# ---- 焦点 / 悬停（浅色主题下的可见反馈）----
#: 选中的缩略图：加粗外框 + 右下角对勾徽标。用 `--accent-deep` 而不是
#: `--accent`：#14B8A6 明度偏高，叠在花图上会被吃掉。
C_FOCUS = QColor("#185FA5")
#: 悬停：淡色描边，用来告诉用户"这里点得动"
C_HOVER = QColor("#8FBCE8")
#: 未获得焦点时，当前分组卡片的描边，只是淡淡一层，不要喧宾夺主
C_CARD_FOCUS = QColor("#B9D3EE")
#: 选中缩略图的半透明覆盖色（alpha 压得很低，不盖住图片内容）
C_FOCUS_WASH = QColor(24, 95, 165, 26)

#: 即时提示气泡的底色 / 描边 / 文字。由 `apply_theme()` 跟着主题刷新。
#: 用「卡片色 + 边框」而不是深色底：它是常驻的，深色块一直杵在界面上
#: 会很压：浅底 + 主题色描边既能一眼认出"这是浮层"，又不抢戏。
C_TIP_BG = QColor("#FFFFFF")
C_TIP_BORDER = QColor("#D9ECE6")
C_TIP_TEXT = QColor("#1D2B28")

#: 主色（流萤 · 薄荷）。同样由 `apply_theme()` 刷新。
C_ACCENT = QColor("#14B8A6")

#: 主色的深一档（`--accent-deep`）。用于渐变起点、选中环、hover 文字。
#: 由 `apply_theme()` 刷新。
C_ACCENT_DEEP = QColor("#0D9488")

#: 「萤火的暖光」端色：品牌渐变收尾、进度条行进段收尾都用它。
#: 不随主题变（角色锚点，见 `theme.BRAND_EMBER`）。
C_EMBER = QColor("#D97706")

#: 卡片投影色。`0 2px 8px rgba(23,108,96,.06)`。
#: 极淡的一层，作用不是"立体感"而是把白卡片从浅色背景里托起来。
#: 没有它时，白卡片贴在近白背景上会显得"糊在一起"、边界全靠描边。
#: QSS 给不了 box-shadow，只能在 delegate 里自绘。
C_SHADOW = QColor(23, 108, 96)

#: 投影的 alpha（0-255）。6% ≈ 15。
C_SHADOW_ALPHA = 15

#: 侧栏面板的竖直渐变三段色（上 = 冷薄荷、中 = 近白、下 = 暖米）。
#: 写成 CSS 是 `linear-gradient(180deg, #E7F8F3, #FBFEFD 55%, #FDF6EA 130%)`。
#: 这是让侧栏"读起来是一块独立面板"的关键，纯色侧栏会跟主画布糊在一起。
#: 由 `apply_theme()` 刷新。
C_PANEL_TOP = QColor("#E7F8F3")
C_PANEL_MID = QColor("#FBFEFD")
C_PANEL_END = QColor("#FDF6EA")

#: 面板描边（比卡片描边略深一点点）
C_PANEL_LINE = QColor("#D9ECE6")

#: 面板投影。`0 6px 24px rgba(23,108,96,.10)`。
#: 比卡片投影重得多（10% vs 6%），因为它是一整块面板、面积大更需要托起。
C_PANEL_SHADOW = QColor(23, 108, 96)
C_PANEL_SHADOW_ALPHA = 26

#: 品牌渐变文字的三段色：
#: `linear-gradient(90deg, accent-deep 20%, accent 55%, amber 110%)`。
#: 这是"流萤"最像角色的一处：冷青过渡到暖琥珀，像萤火从夜色到灯。
#: 由 `apply_theme()` 刷新。
C_BRAND_A = QColor("#0D9488")
C_BRAND_B = QColor("#14B8A6")
C_BRAND_C = QColor("#F59E0B")

BADGE = {
    "exact": [QColor("#E1F5EE"), QColor("#0F6E56")],
    "perceptual": [QColor("#FAEEDA"), QColor("#854F0B")],
    "semantic": [QColor("#E6F1FB"), QColor("#185FA5")],
    "cluster": [QColor("#FCEBEB"), QColor("#A32D2D")],
}


def _rgba_local(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """`#RRGGBB` → `(r, g, b, a)`。

    `theme.py` 里有个同名私有函数，但不要跨模块 import 它 ——
    `widgets` 被 `theme` 反向依赖（`theme.apply_theme` 会操作 `widgets`
    的常量），互相 import 会形成环。两行代码各自留一份更安全。
    """
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def set_accent(hex_color: str) -> None:
    """切主题时刷新主色。

    就地改 `C_ACCENT` 而不是重新赋值：别处已经持有这个对象。
    用 `setRgb` 而不是 `setNamedColor`（后者在 PySide6 6.11 已废弃）。
    """
    c = QColor(hex_color)
    C_ACCENT.setRgb(c.red(), c.green(), c.blue())


def glow(color: QColor, strength: float = 1.0) -> QRadialGradient:
    """萤火光点：内层亮、外层散，用径向渐变模拟 CSS 的双层 box-shadow。

    QSS 不支持 `box-shadow`，所以"流萤"的辨识度只能这样自绘。

    克制使用：只给「品牌圆点 / 主按钮 hover / 进度条行进端」三处。
    用在缩略图边框或列表行上会让图片边界发虚，而用户正是在判断
    "这两张是不是同一张"，视觉干扰是这里最忌讳的东西。
    """
    a = max(0.0, min(1.0, 0.55 * strength))
    g = QRadialGradient(QPointF(0.5, 0.5), 0.5)
    inner = QColor(color)
    inner.setAlphaF(a)
    mid = QColor(color)
    mid.setAlphaF(a * 0.35)
    edge = QColor(color)
    edge.setAlphaF(0.0)
    g.setCoordinateMode(QGradient.ObjectBoundingMode)
    g.setColorAt(0.0, inner)
    g.setColorAt(0.45, mid)
    g.setColorAt(1.0, edge)
    return g



def placeholder_color(path: str) -> QColor:
    """未加载时的占位色：由路径稳定派生，避免整片灰白闪烁。"""
    h = int(hashlib.md5(path.encode("utf-8", "replace")).hexdigest()[:6], 16)
    hue = h % 360
    return QColor.fromHsv(hue, 40, 238)


class GroupDelegate(QStyledItemDelegate):
    """把一行画成一张分组卡片：标签行 + 缩略图条。

    卡片高度随可用宽度变化（缩略图会自动折行），所以窗口缩放时要
    调用 `set_view_width()` 并让视图重新布局。
    """

    def __init__(self, model: GroupModel, thumbs: ThumbCache, view=None):
        super().__init__(view)
        self.model = model
        self.thumbs = thumbs
        #: 承载本 delegate 的 QListView。命中测试要拿它做坐标换算。
        #: 必须显式持有，不能靠 parent()（parent 是窗口，不是视图）。
        self.view = view
        self.view_width = 900
        #: 缩略图边长。做成实例属性（原来是模块常量 `THUMB_SIZE`）是为了
        #: 支持用户拖动滑块调节：模块常量改不动，而十几处引用如果各自
        #: 去查配置，改起来极易漏。统一走这个属性。
        self.thumb_size: int = THUMB_SIZE
        #: 当前选中的缩略图集合 `{(可见行下标, 行内下标), ...}`，空集表示没有。
        #:
        #: 早期是 `sel_vrow/sel_pos` 两个 int，只能选一张。用户要求
        #: Shift 连选多张，所以改成集合。主选中项（鼠标最后点的那张、
        #: 用于右侧详情面板显示）另存 `anchor`。
        self.sel_set: set[tuple[int, int]] = set()
        #: 主选中项：「最后一次点中的那一张」。右侧详情面板显示它。
        self.anchor: Optional[tuple[int, int]] = None
        #: 鼠标悬停的缩略图 `(可见行下标, 行内下标)`；None 表示没有
        self.hover: Optional[tuple[int, int]] = None
        self._font_label = QFont()
        self._font_label.setPointSize(9)
        self._font_label.setBold(True)
        self._font_small = QFont()
        self._font_small.setPointSize(8)

    # ---------------------------------------------------------- 选中 / 悬停
    def set_selected(self, vrow: int, pos: int) -> None:
        """单选：清掉其他选中，只留这一张，并设为主选中项。"""
        self.sel_set = {(vrow, pos)}
        self.anchor = (vrow, pos)

    def set_selection(self, hits: set[tuple[int, int]],
                      anchor: Optional[tuple[int, int]] = None) -> None:
        """多选：整体替换选中集合。"""
        self.sel_set = set(hits)
        self.anchor = anchor if anchor in self.sel_set else (
            next(iter(self.sel_set)) if self.sel_set else None)

    def clear_selected(self) -> None:
        self.sel_set = set()
        self.anchor = None

    def set_hover(self, hit: Optional[tuple[int, int]]) -> None:
        self.hover = hit

    def is_selected(self, vrow: int, pos: int) -> bool:
        return (vrow, pos) in self.sel_set

    def is_anchor(self, vrow: int, pos: int) -> bool:
        """主选中项：画得更重一点，让用户知道"焦点"在哪张。"""
        return self.anchor == (vrow, pos)

    @property
    def sel_vrow(self) -> int:
        """兼容属性：主选中项的行下标（无选中 = -1）。"""
        return self.anchor[0] if self.anchor else -1

    @property
    def sel_pos(self) -> int:
        """兼容属性：主选中项的行内下标（无选中 = -1）。"""
        return self.anchor[1] if self.anchor else -1

    # ---------------------------------------------------------- 布局
    def set_thumb_size(self, size: int) -> bool:
        """改缩略图边长。返回 True 表示确实变了（调用方据此决定是否重布局）。

        `per_line()` / `card_height()` 全都由这个值派生，所以改完必须让
        视图 `doItemsLayout()` 重算行高，否则卡片高度还是旧的，
        新尺寸的缩略图会画到下一张卡片头上。`window._on_thumb_size_changed()`
        负责这件事。
        """
        size = max(64, min(256, int(size)))
        if size == self.thumb_size:
            return False
        self.thumb_size = size
        return True

    def set_view_width(self, width: int) -> bool:
        if abs(width - self.view_width) < 8:
            return False
        self.view_width = width
        return True

    def per_line(self) -> int:
        usable = max(self.thumb_size, self.view_width - CARD_PAD * 2 - 8)
        return max(1, usable // (self.thumb_size + ITEM_GAP))

    def lines_of(self, row) -> int:
        return max(1, math.ceil(row.count / self.per_line()))

    def card_height(self, row) -> int:
        return CARD_PAD * 2 + LABEL_H + 6 + self.lines_of(row) * (
            self.thumb_size + THUMB_GAP + CAPTION_H + 4)

    def sizeHint(self, option, index) -> QSize:
        row = self.model.row_at(index.row())
        if row is None:
            return QSize(0, MIN_CARD_H)
        return QSize(0, self.card_height(row))

    # ---------------------------------------------------------- 命中测试
    def _item_rect(self, card_rect: QRect, row, pos: int) -> QRect:
        """某个缩略图的矩形，坐标空间与传入的 `card_rect` 一致。

        两个调用方给的坐标系不同，所以这个函数只做纯粹的"相对偏移"：

        - `paint()` 传 `option.rect`（已经是视口绝对坐标）；
        - `hit_test()` 传 `QRect(0, 0, w, h)`（把 `visualRect` 平移回原点）。

        早期版本让 `hit_test()` 直接把 `visualRect` 丢进来，于是
        `card_rect.top()` 被二次叠加：表现为「点缩略图正中间没反应，
        偏差正好是一行的纵向位置」。这个坑离屏截图看不出来，只有真点一下才露。
        """
        per = self.per_line()
        x0 = card_rect.left() + CARD_PAD
        line, col = divmod(pos, per)
        x = x0 + col * (self.thumb_size + ITEM_GAP)
        y = (card_rect.top() + CARD_PAD + LABEL_H + 6
             + line * (self.thumb_size + THUMB_GAP + CAPTION_H + 4))
        return QRect(x, y, self.thumb_size, self.thumb_size)

    def hit_test(self, viewport_pos) -> Optional[tuple[int, int]]:
        """视口坐标 → `(可见行下标, 行内下标)`。点空白返回 None。

        刻意不复用 `indexAt()`：那给的是"行"，而一行里可能挂着几十张缩略图。

        这里不能靠 `parent()` 反查视图：delegate 的 parent 是 MainWindow
        （构造时传的是 `self`），不是 QListView。早期版本用 `self.parent()` 然后
        `hasattr(view, "indexAt")` 做守卫，结果是静默返回 None：
        缩略图看着能点，其实一次都没命中，而且不报任何错。
        改为显式持有 view 引用。
        """
        view = self.view
        if view is None:
            return None
        idx = view.indexAt(viewport_pos)
        vrow = idx.row()
        if vrow < 0:
            return None
        row = self.model.row_at(vrow)
        if row is None:
            return None
        # 坐标空间对齐：`visualRect` 给的是绝对视口坐标，而 `_item_rect` 期望
        # "卡片左上角在原点"的坐标系，所以先平移回原点再比较。
        vr = view.visualRect(idx)
        rel = viewport_pos - vr.topLeft()
        card = QRect(0, 0, vr.width(), vr.height())
        for pos in range(row.count):
            if self._item_rect(card, row, pos).contains(rel):
                return (vrow, pos)
        return None

    # ---------------------------------------------------------- 绘制
    def paint(self, painter: QPainter, option, index) -> None:
        row = self.model.row_at(index.row())
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        vrow = index.row()
        rect = option.rect.adjusted(6, 4, -6, -4)
        self._draw_card_bg(painter, rect, row, focus=bool(option.state
                                                          & QStyle.State_HasFocus))
        y = rect.top() + CARD_PAD
        self._draw_header(painter, rect, y, row)
        y += LABEL_H + 6
        self._draw_strip(painter, rect, y, row, vrow)

        painter.restore()

    def _draw_card_bg(self, painter: QPainter, rect: QRect, row,
                      focus: bool = False) -> None:
        #: 先铺垫投影再画卡片本体。QPainter 没有 box-shadow，
        #: 用两层错位圆角矩形模拟：外层更大更淡、内层更小更实。
        #: 只叠两层是因为卡片是列表项、一屏可能有十几个。
        #: 层数多了每帧的重绘成本会上去：
        #: 两个无限浮动的动画小点就能吃掉一整核 CPU，别重蹈覆辙。
        self._draw_card_shadow(painter, rect)

        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        painter.fillPath(path, QBrush(C_CARD))
        if row.oversized:
            pen = QPen(QColor("#E24B4A"), 1.4)
            pen.setStyle(Qt.DashLine)
        elif focus:
            # 只画 1.6px，不画成 2px 实框：当前行有键盘焦点，和"选中某张图"
            # 是两个层级的反馈，强了会让人以为整组被选中了。
            pen = QPen(C_CARD_FOCUS, 1.6)
        else:
            pen = QPen(C_BORDER, 1.0)
        painter.setPen(pen)
        painter.drawPath(path)

    def _draw_card_shadow(self, painter: QPainter, rect: QRect) -> None:
        """卡片下缘的柔和投影（box-shadow 的等价物）。

        用 alpha 自乘而不是固定衰减：`C_SHADOW_ALPHA` 是基准层，
        每往外一层 alpha × 0.42：这样即使主题把基准调大，
        外层也不会突然变得很脏。
        """
        if C_SHADOW_ALPHA <= 0:
            return
        painter.save()
        painter.setPen(Qt.NoPen)
        base = C_SHADOW_ALPHA
        for i in (2, 1):
            a = int(base * (0.42 ** (3 - i)))
            if a <= 0:
                continue
            c = QColor(C_SHADOW)
            c.setAlpha(a)
            painter.setBrush(QBrush(c))
            spread = i
            # 只向下偏移 1px：光源在上方，四边均匀偏移看起来像"发光"
            r = rect.adjusted(-spread, -spread + 1, spread, spread + 1)
            painter.drawRoundedRect(r, 10 + spread, 10 + spread)
        painter.restore()

    def _draw_header(self, painter: QPainter, rect: QRect, y: int, row) -> None:
        painter.setFont(self._font_label)
        painter.setPen(C_TEXT)
        n = row.count
        title = f"组 · {n} 张"
        if row.oversized:
            title = f"聚类 · {n} 张（不允许批量删除）"
        fm = QFontMetrics(self._font_label)
        w = fm.horizontalAdvance(title)
        painter.drawText(rect.left() + CARD_PAD, y, w + 4, LABEL_H,
                         Qt.AlignVCenter | Qt.AlignLeft, title)

        bg, fg = BADGE.get(row.kind, BADGE["perceptual"])  # list，非 tuple
        badge = self._badge_text(row)
        bw = QFontMetrics(self._font_small).horizontalAdvance(badge) + 16
        br = QRect(rect.left() + CARD_PAD + w + 10, y + 3, bw, LABEL_H - 6)
        self._rounded(painter, br, 4, bg)
        painter.setFont(self._font_small)
        painter.setPen(fg)
        painter.drawText(br, Qt.AlignCenter, badge)

        reclaim = sum(i.size for p, i in enumerate(row.items)
                      if not row.is_kept(p) and i.decision == DELETE)
        if reclaim:
            painter.setFont(self._font_small)
            painter.setPen(C_TEXT_DIM)
            painter.drawText(br.right() + 10, y, rect.right() - br.right() - 20,
                             LABEL_H, Qt.AlignVCenter | Qt.AlignLeft,
                             f"可释放 {reclaim / 1024**2:.1f} MB")

    @staticmethod
    def _badge_text(row) -> str:
        if row.oversized:
            return "聚类 · 需人工确认"
        if row.kind == "exact":
            return "字节完全相同"
        if row.kind == "semantic":
            return "语义相似"
        # 感知组建行时没传 label_of，`row.label` 就是 kind 字符串本身。
        # 直接画出来会在界面上露出英文 "perceptual"，所以把它当成"没给文案"。
        if row.label and row.label != row.kind:
            return row.label
        return "感知相似"

    def _draw_strip(self, painter: QPainter, rect: QRect, y0: int, row,
                    vrow: int = -1) -> None:
        per = self.per_line()
        x0 = rect.left() + CARD_PAD
        for pos, item in enumerate(row.items):
            line, col = divmod(pos, per)
            x = x0 + col * (self.thumb_size + ITEM_GAP)
            y = y0 + line * (self.thumb_size + THUMB_GAP + CAPTION_H + 4)
            if y + self.thumb_size > rect.bottom():
                break
            self._draw_item(painter, x, y, item, row.is_kept(pos), item.decision,
                            selected=self.is_selected(vrow, pos),
                            anchor=self.is_anchor(vrow, pos),
                            hovered=(self.hover == (vrow, pos)),
                            index=pos, total=row.count)

    def _draw_item(self, painter: QPainter, x: int, y: int, item, is_keep: bool,
                   decision: str, *, selected: bool = False,
                   anchor: bool = False,
                   hovered: bool = False, index: int = -1, total: int = 0) -> None:
        box = QRect(x, y, self.thumb_size, self.thumb_size)
        pix = self.thumbs.get(item.path)
        if pix is None:
            # 未命中：画占位色块 + 发起异步加载。绝不在这里读文件。
            painter.fillRect(box, QBrush(placeholder_color(item.path)))
            self.thumbs.request(item.path)
        else:
            painter.drawPixmap(box, pix.scaled(box.size(), Qt.KeepAspectRatioByExpanding,
                                               Qt.SmoothTransformation))

        # 选中强调放在裁决边框之下、绘制在最上层，这样无论这张图是
        # 待删（红虚线）还是保留（绿实线），用户都能一眼看出"我点的是这张"。
        if selected:
            painter.fillRect(box, QBrush(C_FOCUS_WASH))

        if is_keep:
            painter.setPen(QPen(C_KEEP, 2.0))
            painter.drawRect(box.adjusted(1, 1, -1, -1))
        elif decision == DELETE:
            pen = QPen(C_DEL, 1.6)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(box.adjusted(1, 1, -1, -1))
        else:
            painter.setPen(QPen(C_UNDECIDED, 1.0))
            painter.drawRect(box.adjusted(1, 1, -1, -1))

        if selected:
            self._draw_selection_ring(painter, box, anchor=anchor)
        elif hovered:
            painter.setPen(QPen(C_HOVER, 1.4))
            painter.drawRect(box.adjusted(2, 2, -2, -2))

        if is_keep:
            cap = "保留"
        elif decision == DELETE:
            cap = "待删"
        else:
            cap = "未裁决"
        color = C_KEEP if is_keep else (C_DEL if decision == DELETE else C_TEXT_DIM)
        if selected:
            # 选中时把「第 n 张」也说出来：右侧详情面板会显示同一编号，
            # 两处对得上，用户才敢按"删除这一张"。
            cap = f"第 {index + 1}/{total} 张"
            color = C_FOCUS
        painter.setFont(self._font_small)
        painter.setPen(color)
        painter.drawText(QRect(x, y + self.thumb_size + 2, self.thumb_size, CAPTION_H),
                         Qt.AlignHCenter | Qt.AlignTop, cap)

    @staticmethod
    def _draw_selection_ring(painter: QPainter, box: QRect,
                             anchor: bool = True) -> None:
        """选中环：外扩 1px 的双层描边 + 右下角对勾徽标。

        只靠一层蓝线在浅色主题里太弱（缩略图本身就很花），
        所以再加一个对勾：它是形状差异，不依赖颜色也能分辨。

        `anchor=False` 用于多选里的非主选中项：线细一半、不画对勾。
        否则一次选了 20 张，20 个对勾会让用户完全分不清"我最后点的是哪张"，
        而右侧面板说的又是主选中项，两边就对不上了。
        """
        if anchor:
            outer = box.adjusted(-3, -3, 3, 3)
            painter.setPen(QPen(C_FOCUS, 2.4))
        else:
            outer = box.adjusted(-2, -2, 2, 2)
            painter.setPen(QPen(C_FOCUS, 1.6, Qt.DotLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(outer)

        if not anchor:
            return

        r = 18
        badge = QRect(outer.right() - r + 1, outer.bottom() - r + 1, r, r)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(C_FOCUS))
        painter.drawEllipse(badge)

        painter.setBrush(Qt.NoBrush)
        pen = QPen(QColor("#FFFFFF"), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        cx, cy = badge.center().x(), badge.center().y()
        path = QPainterPath()
        path.moveTo(cx - 4.5, cy + 0.2)
        path.lineTo(cx - 1.4, cy + 3.4)
        path.lineTo(cx + 4.6, cy - 3.4)
        painter.drawPath(path)

    @staticmethod
    def _rounded(painter: QPainter, rect: QRect, radius: int, color: QColor) -> None:
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QBrush(color))



# ================================================================ 下拉框行样式

class ComboItemDelegate(QStyledItemDelegate):
    """给 `QComboBox` 的弹出列表画行：自己画选中底，不靠 QSS。

    为什么非要自己画，这是 Qt 样式表一个很难发现、且绕不过去的坑：

      QComboBox 的弹出列表要走"原生 style + palette"渲染，
      而只要 QComboBox 这个控件本身被任何 QSS 规则命中
      （本项目有 `QLineEdit, QComboBox {{ padding...; border...; }}`），
      popup 的选中行就不再采纳 `::item:selected {{ background: ... }}`，
      也不再采纳 view 的 palette.Highlight。

    离屏逐像素实测（三行分别取行中部底色，期望中间那行 = `#dcf5ef`）：

      · 仅 `QComboBox QAbstractItemView::item:selected {{ background }}`
        → 选中行还是 Qt 默认灰描边 + 白底（`#a0a0a0` / `#ffffff`）
      · 再叠上 `QWidget {{ ... }}` 这条通配规则
        → 整行全白，连默认描边都没了
      · 给 view 设 `palette.Highlight = soft`
        → palette 值确实写进去了，但画面不变（该路径已被绕过）
      · 加 `selection-background-color` / `:active` / `:!active` 各种变体
        → 全部无效
      · 换成这个 delegate 手动 fillPath
        → 选中行稳定 `#dcf5ef`

    这几条是在"完整主题 QSS + offscreen 截图采样"下反复验证过的，
    不是推测。所以选中行必须由 delegate 来画。

    颜色不是硬编码的：读的是模块级 `C_TIP_*` 之外的 `_SEL_*`，
    由 `theme.apply_theme()` 跟着主题刷新（见 `theme._apply_combo_style`）。
    """

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        r = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # 底色：选中 > 悬停 > 无
        if selected:
            bg = _DELEGATE_SEL_BG
        elif hovered:
            bg = _DELEGATE_HOVER_BG
        else:
            bg = None
        if bg is not None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(bg))
            # 左右各留 2px，行高比 rect 略收 1px：圆角行块才不会糊到边框上
            painter.drawRoundedRect(r.adjusted(2, 1, -2, -1), 6, 6)
            if selected:
                painter.setPen(QPen(_DELEGATE_SEL_LINE, 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(r.adjusted(2, 1, -2, -1), 6, 6)

        painter.setPen(_DELEGATE_SEL_TEXT if selected else _DELEGATE_TEXT)
        font = painter.font()
        font.setBold(selected)
        painter.setFont(font)
        # 左边距 12px 和 QSS 里的 `padding: 2px 10px` 对齐，换文字也不跳
        painter.drawText(
            r.adjusted(12, 0, -8, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
        painter.restore()


#: 弹出列表行的配色。由 `theme.apply_theme()` 就地刷新（同其它 `C_*` 常量）。
_DELEGATE_SEL_BG = QColor("#DCF5EF")
_DELEGATE_SEL_TEXT = QColor("#0D9488")
_DELEGATE_SEL_LINE = QColor("#14B8A6")
_DELEGATE_HOVER_BG = QColor("#F6FBF9")
_DELEGATE_TEXT = QColor("#1D2B28")


# ================================================================ 即时提示

class InstantToolTip(QWidget):
    """即时弹出、且不会自动消失的提示气泡。

    为什么不用 `QWidget.setToolTip()`：那个是 Qt 的内建机制，有两个改不掉的
    行为 ——

      1. 悬停约 1 秒才弹（`QApplication.toolTipWakeUp` 的默认延迟）。
         用户对着一个不理解的下拉框悬停，等一秒才看到解释，体验是断裂的。
      2. 弹出后约 10 秒自动消失（`toolTipFallAsleep`）。
         正读到一半提示没了，得移开再悬停一次。

    `setToolTip` 只能用 `QToolTip.setFont()` 之类改外观，改不了这两条时序，
    所以这里干脆自己画一个。

    挂载方式：`tooltip.attach(widget, text_provider)` ——
    装上事件过滤器后，鼠标进入 / 获得焦点即弹，离开 / 失焦即关。
    `text_provider` 可以是个 `str`，也可以是 `callable() -> str`
    （文案会变时用后者，比如模式说明随下拉框切换更新）。
    """

    #: 气泡相对锚点控件的偏移（正数 = 往下偏一点，别贴着脸）
    OFFSET_Y = 6
    #: 距屏幕边缘的最小留白，防止气泡被裁出屏幕外
    SCREEN_MARGIN = 6
    #: 气泡最大宽度。再宽就要来回扫视才能读完，反而不如折行。
    MAX_W = 380
    #: 内边距
    PAD_X = 11
    PAD_Y = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        # 必须是 `Qt.ToolTip` 而不是 `Qt.Popup`/`Qt.Tool`：
        #   · `Popup` 会抢走鼠标（弹出后控件收到 Leave，气泡自己把自己关了）；
        #   · `ToolTip` 是"非激活窗口"，不参与焦点链，也不会出现在任务栏。
        # 另外必须设 `WA_ShowWithoutActivating`：否则显示时会把主窗口的
        # 键盘焦点抢走，用户正在输入就会被打断。
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint
                            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        #: 不需要鼠标事件：让它"透明"，鼠标穿透到下面的控件
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._text: str = ""
        self._owner: Optional[QWidget] = None
        self._provider = None
        #: 多行文本的排版结果，`paintEvent` 直接照着画
        self._lines: list = []
        self.hide()

    # ------------------------------------------------------------ 挂载
    def attach(self, widget: QWidget, text) -> None:
        """给 `widget` 装上即时提示。`text` 是字符串或返回字符串的函数。"""
        self._provider = text
        if not hasattr(widget, "_instant_tooltips"):
            widget._instant_tooltips = []
        widget._instant_tooltips.append(self)
        widget.installEventFilter(_InstantToolTipFilter(self))
        # 控件被销毁时气泡跟着销毁，避免残留一个孤儿窗口
        widget.destroyed.connect(self._on_owner_destroyed)

    def _on_owner_destroyed(self, *_a) -> None:
        self._owner = None
        self.hide()

    # ------------------------------------------------------------ 显隐
    def _current_text(self) -> str:
        t = self._provider
        if t is None:
            return ""
        try:
            t = t() if callable(t) else t
        except Exception:  # noqa: BLE001
            return ""
        return str(t or "").strip()

    def popup_for(self, widget: QWidget) -> None:
        """在 `widget` 下方弹出。文案为空则什么都不做。"""
        txt = self._current_text()
        if not txt:
            self.hide()
            return
        self._owner = widget
        self._text = txt
        self._relayout(txt)
        self._place(widget)
        self.show()
        self.raise_()

    def dismiss(self, widget: Optional[QWidget] = None) -> None:
        """收起。传 `widget` 时只在当前锚点确实是它才关
        （避免 A 的离开事件把 B 刚弹出的气泡关掉）。"""
        if widget is not None and self._owner is not widget:
            return
        self._owner = None
        self.hide()

    # ------------------------------------------------------------ 排版
    def _relayout(self, txt: str) -> None:
        """把文本按 `MAX_W` 折行，并算出气泡尺寸。

        必须比 `QFontMetrics.elidedText` 那种"截断"更友好：
        说明文字被砍掉尾巴就等于没说。宁可多占几行也不截断。
        但也不能无限宽：撑满屏幕的提示条读起来要来回扫视。
        """
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF()))
        self.setFont(f)
        fm = QFontMetrics(f)
        max_w = self.MAX_W - self.PAD_X * 2

        lines: list = []
        for raw in txt.split("\n"):
            if not raw:
                lines.append("")
                continue
            cur = ""
            for ch in raw:
                if fm.horizontalAdvance(cur + ch) > max_w and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            lines.append(cur)
        self._lines = lines
        w = max((fm.horizontalAdvance(ln) for ln in lines), default=0)
        self.setFixedSize(w + self.PAD_X * 2,
                          fm.height() * len(lines) + self.PAD_Y * 2)

    def _place(self, widget: QWidget) -> None:
        """摆在锚点控件正下方；贴边时自动翻转/收进来。"""
        try:
            anchor = widget.mapToGlobal(QPoint(0, widget.height()))
        except Exception:  # noqa: BLE001
            anchor = widget.pos()
        x = anchor.x()
        y = anchor.y() + self.OFFSET_Y

        scr = widget.screen().availableGeometry() if widget.screen() else None
        if scr is not None:
            # 右边界溢出 → 左移贴回屏内
            if x + self.width() > scr.right() - self.SCREEN_MARGIN:
                x = scr.right() - self.SCREEN_MARGIN - self.width()
            if x < scr.left() + self.SCREEN_MARGIN:
                x = scr.left() + self.SCREEN_MARGIN
            # 下边界溢出 → 翻到控件上方
            if y + self.height() > scr.bottom() - self.SCREEN_MARGIN:
                y = widget.mapToGlobal(QPoint(0, 0)).y() \
                    - self.height() - self.OFFSET_Y
        self.move(int(x), int(y))

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        # 用主题 token 画，跟菜单/卡片同一个视觉语言（见 theme.apply_theme
        # 里对 C_PANEL_* 的刷新）
        painter.setPen(QPen(C_TIP_BORDER, 1))
        painter.setBrush(QBrush(C_TIP_BG))
        painter.drawRoundedRect(r, 9, 9)
        painter.setPen(C_TIP_TEXT)
        fm = QFontMetrics(self.font())
        y = self.PAD_Y
        for ln in self._lines:
            painter.drawText(QRect(self.PAD_X, y, self.width() - self.PAD_X * 2,
                                   fm.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, ln)
            y += fm.height()
        painter.end()


class _InstantToolTipFilter(QObject):
    """把锚点控件的 进入/离开/焦点 事件翻译成气泡的 弹/收。"""

    def __init__(self, tip: "InstantToolTip", parent=None):
        super().__init__(parent)
        self.tip = tip

    def eventFilter(self, obj, event) -> bool:
        et = event.type()
        if et == QEvent.Enter:
            self.tip.popup_for(obj)
        elif et == QEvent.Leave:
            self.tip.dismiss(obj)
        elif et in (QEvent.FocusIn, QEvent.Show):
            # 键盘走到这里（Tab 聚焦）也要弹：只用鼠标能看见等于键盘用户看不到
            if obj.hasFocus():
                self.tip.popup_for(obj)
        elif et in (QEvent.FocusOut, QEvent.Hide):
            self.tip.dismiss(obj)
        return False


# ================================================================ 预览框

#: 动图解码的进度 token。用 `itertools.count` 而不是普通 int：
#: 主线程加、后台线程回调里读，`+= 1` 在多线程下会丢 update。
_ANIM_TOKEN = itertools.count(1)


def _next_anim_token() -> int:
    return next(_ANIM_TOKEN)


#: 已经"没人要"、但还没跑完的解码线程。必须模块级持有。
#:
#: 为什么不能让它自生自灭：`QThread` 对象一旦在 `isRunning()` 时被销毁，
#: Qt 会直接 `abort()`："QThread: Destroyed while thread is still running"。
#: 这条没有 Python 回溯，退出码是 127，从用户视角就是"莫名闪退"。
#: 所以超时没等到收工的线程都挪到这里，等它自己 `finished` 再放掉。
_ANIM_PARKED: list = []


def _park_anim_task(task) -> None:
    """把还没收工的线程挪到模块级列表，等它结束再释放。

    必须处理"连接之前线程就已经收工"这个竞争。
    调用方（`_retire_anim_task` / `shutdown_anim`）都是先看 `isRunning()`
    再进来连接：这两步之间线程完全可能已经跑完并发出过 `finished`。
    信号只发一次，连接建在之后 = 永远不会被触发 = 条目永久留在停车场
    （实测：快速切换几百次后停车场卡在 1 不再回落，线程和它占的
    句柄再也放不掉；进程退出时集中销毁更是有 abort 风险）。

    所以连接完再查一次：已经不跑了就手动放掉。`_release` 幂等，
    与"连接后信号才到"的情况重叠也不会重复处理。
    """
    if task in _ANIM_PARKED:
        return
    _ANIM_PARKED.append(task)

    def _release(t=task) -> None:
        # 先断连接，否则这个闭包（连带 t）会被信号一直持有
        try:
            t.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            _ANIM_PARKED.remove(t)
        except (ValueError, RuntimeError):
            pass        # 已经被放掉 / C++ 侧已析构：都是幂等情形

    task.finished.connect(_release)
    # 连接之前就收工了 → `finished` 不会再发一次，必须手动放掉
    if not task.isRunning():
        _release()


def _retire_anim_task(task) -> None:
    """释放一个已经没人要的解码线程。

    绝不能直接丢引用。 这是"切几次 GIF 就闪退"的真正根因
    （本轮才定位到，上一轮修的是另一个泄漏问题）：

    `_AnimDecodeTask.run()` 的最后一句是 `self.done.emit(...)`。
    emit 走队列连接，投递完就立刻返回，于是 `run()` 返回、
    主线程可能马上就处理到那条消息。但此时线程还没真正结束，
    `isRunning()` 仍是 `True`（实测：`run()` 返回时 `isRunning=True`，
    3 ms 后 `_on_anim_ready` 才被调用）。

    如果这时把最后一个 Python 引用丢掉 → GC 销毁 QThread 对象 →
    Qt 发现"运行中的线程被销毁" → 直接 `abort()`：

      · 没有 Python 回溯
      · 没有错误弹窗
      · 退出码 127

    从用户视角就是"切几下就没了"。平时这个窗口只有几毫秒，
    但那份 60 帧 / 5.6 MB 的动图解码要 800 ms、而且同组有 4 张一样的，
    并发解码一多，窗口就很容易被撞上，所以"切 3~4 次"就能复现。

    做法：还在跑就交给 `_ANIM_PARKED`（模块级强引用）等它自己结束；
    `isRunning()` 为假说明线程已彻底终止，那时丢引用才是安全的。
    """
    if task.isRunning():
        _park_anim_task(task)


def _pixmap_from_rgb(arr) -> QPixmap:
    """`np.ndarray(H, W, 3)` → `QPixmap`。decode 层返回的一律是 RGB888。"""
    h, w, ch = arr.shape
    img = QImage(arr.data, w, h, ch * w, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(img)


class _AnimDecodeTask(QThread):
    """后台解一个动图，完成后把 `Animation` 通过信号送回主线程。

    不要用 `QTimer.singleShot` 从后台线程回主线程。 见
    `PreviewPane._start_anim_if_needed` 的说明：那样回调永远不执行。

    用 `QThread` 而不是 `threading.Thread` + 裸回调：`Signal` 的跨线程
    投递由 Qt 自己保证（`QueuedConnection`），不需要我们再手搓
    `QMetaObject.invokeMethod` 之类的桥。
    """

    #: (token, Animation|None)
    done = Signal(int, object)

    def __init__(self, path: str, token: int, parent=None):
        super().__init__(parent)
        self.path = path
        self.token = token

    def run(self) -> None:
        anim = None
        try:
            from imgdedup_core import decode as D

            anim = D.load_animation(self.path)
        except Exception:  # noqa: BLE001
            anim = None
        # 跨线程 emit：Qt 自动排进接收者（主线程）的事件队列
        self.done.emit(self.token, anim)


class PreviewPane(QWidget):
    """预览框：显示当前主选中那张图的大图。放在右侧边栏。

    用独立的 `ThumbCache`，不复用网格那个。 网格要的是 112px 级的小图、
    缓存 800 张；预览要的是 `STILL_SIDE`(1024px) 级的大图，一张就顶网格几十张
    的内存。混用会互相挤掉缓存：逛一圈预览就能把网格缓存冲空，滚动时整片重新加载。

    未命中时先画占位、不阻塞。大图解码是百毫秒级的（尤其大 JPEG，
    熵解码必须解完全部系数），同步读会让点一下卡一下。和网格同一套异步套路。

    必须 `shutdown()`（由主窗口 `closeEvent` 调用），否则后台任务回来
    emit 时信号已销毁：见 `ThumbCache.shutdown()` 的说明。

    只画图，不在图上叠文字。 文件名 / 尺寸 / 格式这些信息由右侧栏
    下方的标签负责（预览框就紧挨着它们）。早先预览框在中间下方时自带
    caption/sub 叠字，挪到窄侧栏后那些字会和下方标签重复、还容易压住图。
    `caption`/`sub` 参数保留只为不改动既有调用点。

    ---- 滚轮缩放（Task #6）----

    `self._zoom` 是显示倍率，只作用于 `paintEvent` 里的 `scaled()`，
    原图与缓存里的 `QPixmap` 一个字节都不动：缩到 0.2x 再放回 1x，
    画质无损。倍率夹在 `ZOOM_MIN` ~ `ZOOM_MAX`。

    滚轮事件这里必须 `accept()`：外层 `QScrollArea`（右栏）默认会把
    滚轮吞掉去滚自身，不 accept 的话滚轮先滚栏、图完全不缩放。

    缩放以光标位置为锚点：光标停在图的某个局部，放大后那个局部还在
    原地。这不是"锦上添花"—— 侧栏窄，不锚定光标的话放大后想看的细节
    会立刻跑出可视区，用户只能反复"放大→找不到→缩小"。

    ---- 百分比输入框 / 重置按钮 ----

    滚轮和键盘是"相对"操作：滚多少格、按几下，用户记不住当前到底多少倍。
    所以预览框正下方有一行控件（在 `window._build_zoom_row()`）：
    百分比输入框 + 重置按钮。

    两边靠 `zoomChanged` 信号保持同步：滚轮改了倍率，输入框跟着变；
    输入框提交了数值，画面跟着变。没有这条信号，输入框会停在用户上次
    输入的数值上，和实际画面脱节（看起来像"输入框坏了"）。

    语义提醒：1.0（100%）是"适应窗口"，不是"原始像素 1:1"。
    预览框先把图等比缩到装得下，倍率是在那之上再乘的系数。
    UI 上的提示文案要照这个说，否则用户会按"100% = 原图大小"去理解。

    ---- 动图播放（Task #7）----

    解码走 `imgdedup_core.decode.load_animation()`，必须在后台线程：
    几十帧、每帧几十毫秒，同步解会让界面直接卡死。

    播放用 `QTimer`，每帧重启一次、间隔取该帧自己的 duration（`setInterval`
    一次性设不了逐帧不同的时长）。切图 / 清空 / 控件销毁都要 `_stop_anim()`：
    不停定时器的话，图都已经换了旧帧还在往上刷。

    必须有帧数上限（`ANIM_MAX_FRAMES`）：几千帧的 WebP 全解到内存
    是 GB 级的，`load_animation` 超限时会截断并给 `note`，这里照实显示。
    """

    #: 缩放下限 / 上限。 下限留 0.2x 够看清整体构图，上限 16x 够看单像素级细节。
    ZOOM_MIN = 0.2
    ZOOM_MAX = 16.0
    #: 每一格滚轮的缩放系数（1.15 ≈ 滚 5 格翻倍，手感比 1.1 跟手、比 1.25 细腻）
    ZOOM_STEP = 1.15
    #: 单张静态大图的解码边长上限
    STILL_SIDE = 1024

    #: 倍率变化广播（滚轮 / 键盘 / 重置 / 切图归位都会发）。
    #: 右侧栏那个百分比输入框靠它保持同步：没有这条信号，
    #: 输入框就会一直停在"用户上次输入的数值"，和实际画面脱节。
    zoomChanged = Signal(float)

    def __init__(self, cache: "ThumbCache", parent=None):
        super().__init__(parent)
        self.cache = cache
        self._path: Optional[str] = None
        self._pixmap: Optional[QPixmap] = None
        self._token: Optional[int] = None
        #: 保留字段：不再绘制，仅为兼容既有调用点
        self._caption: str = ""
        self._sub: str = ""
        self._zoom: float = 1.0
        #: 放大超过 1x 时平移的偏移（像素，预览框坐标系）
        self._pan = QPointF(0.0, 0.0)
        self._panning = False
        self._pan_start = QPointF(0.0, 0.0)
        self._pan_origin = QPointF(0.0, 0.0)
        # ---- 动图状态 ----
        self._anim: Optional[object] = None      # Animation
        self._anim_frames: list = []             # list[QPixmap]
        self._anim_delays: list = []
        self._anim_idx: int = 0
        self._anim_token: Optional[int] = None   # 在途解码 token
        self._anim_note: str = ""
        self._anim_paused: bool = False
        #: 在途解码任务的强引用：不持有的话 Python 侧会先 GC，
        #: 线程回来 emit 时对象已析构（`RuntimeError: Signal source deleted`）。
        self._anim_tasks: set = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._next_frame)
        # 侧栏只有 260px 宽、还要减去边距，最小值给 240 会撑破布局。
        self.setMinimumSize(150, 150)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)      # 接键盘 +/-/0
        cache.loaded.connect(self._on_loaded)

    # ------------------------------------------------------------ 数据
    def show_path(self, path: Optional[str], caption: str = "",
                  sub: str = "") -> None:
        """切换到大图。`path=None` 表示回到"空"状态。

        "路径没变"的短路不能把清理逻辑一起短路掉。 早先的写法是
        `if path == self._path: return`，于是 `show_path(None)` 在
        `self._path` 已经是 `None` 时直接返回：可此时动图定时器还在跑
        （先 `_feed` 了动图、`_path` 仍是 `None`），结果就是"图没了但帧还在刷"。
        所以短路的条件必须加上"当前没有动图在播"。
        """
        self._caption = caption
        self._sub = sub
        if path == self._path and not self._timer.isActive() \
                and self._anim_token is None:
            self.update()
            return
        self._path = path
        self._pixmap = None
        self._token = None
        self._stop_anim()
        self._reset_view()
        if path:
            if self._start_anim_if_needed(path):
                self.update()
                return
            pm = self.cache.get(path)
            if pm is not None:
                self._pixmap = pm
            else:
                self._token = self.cache.request(path)
        self.update()

    def _on_loaded(self, token: int) -> None:
        if token != self._token:
            return                      # 已经切到别的图了，这个结果作废
        self._token = None
        self._pixmap = self.cache.get(self._path or "")
        self.update()

    # ------------------------------------------------------------ 动图
    def _start_anim_if_needed(self, path: str) -> bool:
        """是动图就在后台开始解码，返回 True 表示"走动图分支，先别走静图"。

        回主线程必须走 `Signal`，不能用 `QTimer.singleShot`。
        这是本项目踩过的一个坑：早先在后台线程里写
        `QTimer.singleShot(0, lambda: self._on_anim_ready(...))`，
        回调永远不执行，因为 `QTimer` 依赖当前线程的事件循环，
        而后台线程根本没有事件循环，那个 0ms 定时器就永远挂着。
        症状是"动图标签一直是加载中"，测起来像解码失败、其实解码完全正常。

        `_AnimDecodeTask` 是 `QObject` 且 `moveToThread` 过吗？不用 ——
        Qt 的队列信号本身就是线程安全的投递机制：跨线程 emit 时
        `QObject::connect` 默认 `AutoConnection` 会转成 `QueuedConnection`，
        把调用排进接收者（本控件，活在主线程）的事件队列。这正是我们要的。
        """
        try:
            from imgdedup_core import decode as D
        except Exception:  # noqa: BLE001
            return False
        if not D.is_animated(path):
            return False                # 扩展名不像 → 不用起线程，静图路径足够
        # 已经在为同一个文件解码 → 复用那个任务，别再起一个线程。
        # 这一条把"来回切两个 GIF"的线程数从"每次一个"压到最多两个，
        # 是用户报的卡顿场景的直接解法。复用还顺带白赚一个好处：
        # 切回来时不必等第二遍解码，结果一到就直接显示。
        #
        # 为什么复用是安全的：`_anim_tasks` 里的任务，要么 `done` 还没发，
        # 要么已发但还在队列里等投递（投递后 `_on_anim_ready` 会立刻把它
        # 摘掉）。所以把它设成当前 token，那次投递就会正确地把动画装上。
        for t in self._anim_tasks:
            if t.path == path:
                self._anim_token = t.token
                self._anim_note = ""
                return True
        self._anim_token = _next_anim_token()
        token = self._anim_token
        self._anim_note = ""
        task = _AnimDecodeTask(path, token)
        task.done.connect(self._on_anim_ready)
        # 任务对象必须留个引用，否则线程还在跑、Python 侧先被 GC，
        # 回来 emit 时对象已析构（而且"运行中的 QThread 被销毁"会让 Qt abort）。
        # 回收点只有 `_on_anim_ready`（它必然被调用，因为信号一直连着）；
        # 控件销毁那条路径由 `shutdown_anim()` 负责。
        self._anim_tasks.add(task)
        task.start()
        return True

    def _on_anim_ready(self, token: int, anim) -> None:
        """后台解码完成。token 不匹配说明用户已经切走了。"""
        # 摘掉任务引用（可能已随 `_stop_anim` 清过，用 discard 幂等）
        #
        # 摘引用之前必须过 `_retire_anim_task`。
        # 这时 `run()` 虽然已经返回，但线程往往还没真正结束
        # （`isRunning()` 仍是 True）。直接丢引用 = 销毁运行中的 QThread
        # = Qt 直接 abort（没有回溯、退出码 127、看起来就是"闪退"）。
        # 这是"切 3~4 次 GIF 就闪退"的真正根因，详见该函数的说明。
        stale = [t for t in self._anim_tasks if t.token == token]
        self._anim_tasks = {t for t in self._anim_tasks if t.token != token}
        for t in stale:
            _retire_anim_task(t)
        if token != self._anim_token:
            return
        self._anim_token = None
        # 不是动图（单帧 GIF / 解不出来）→ 退回静图路径，别留一块空白
        if anim is None or not getattr(anim, "frames", None):
            self._anim = None
            self._anim_frames = []
            self._anim_delays = []
            if self._path:
                pm = self.cache.get(self._path)
                if pm is not None:
                    self._pixmap = pm
                else:
                    self._token = self.cache.request(self._path)
            self.update()
            return
        self._anim = anim
        self._anim_note = getattr(anim, "note", "") or ""
        self._anim_frames = [_pixmap_from_rgb(f) for f in anim.frames]
        self._anim_delays = list(anim.delays)
        self._anim_idx = 0
        self._pixmap = self._anim_frames[0] if self._anim_frames else None
        self._reset_view()
        if not self._anim_paused:
            self._schedule_next()
        self.update()

    def _schedule_next(self) -> None:
        """按当前帧的时长排下一帧。"""
        if self._anim_paused or len(self._anim_frames) < 2:
            return
        ms = self._anim_delays[self._anim_idx] if self._anim_delays else 100
        self._timer.start(max(20, int(ms)))

    def _next_frame(self) -> None:
        if len(self._anim_frames) < 2:
            return
        self._anim_idx = (self._anim_idx + 1) % len(self._anim_frames)
        self._pixmap = self._anim_frames[self._anim_idx]
        self.update()
        self._schedule_next()

    def _stop_anim(self) -> None:
        """停掉播放并作废在途解码。切图 / 清空 / 销毁都走这里。

        绝对不能在这里 `disconnect` 在途任务的 `done` 信号。
        这是"快速切 GIF → 卡住 → 闪退"的根因（本轮修的真 bug）：

        `_anim_tasks` 里唯一会"摘掉任务"的地方是 `_on_anim_ready`（按 token 删）。
        而 `_on_anim_ready` 是 `done` 的槽：信号一断，它永远不会被调用，
        于是 `_anim_tasks` 每切一次图涨一个，且每个都是仍在跑的 QThread。

        实测：来回切 16 次 → 16 个线程堆在里面全在跑。后果两条，
        正好对上用户报的"卡住 + 闪退"：
          1. 卡住：并发解码线程数随切换次数线性增长，互相抢 GIL/CPU；
          2. 闪退：`QThread` 对象在 `isRunning()` 时被销毁 → Qt 直接
             `abort()`，没有 Python 回溯、退出码 127，看起来像"莫名闪退"。

        正确做法是让信号照常送达：token 已经作废，`_on_anim_ready`
        第一行就会把它从 `_anim_tasks` 摘掉，然后因为 token 不匹配而直接
        return：结果被丢掉，但线程被回收了。作废靠 token，
        不靠断信号。
        """
        self._timer.stop()
        self._anim_token = None
        self._anim = None
        self._anim_frames = []
        self._anim_delays = []
        self._anim_idx = 0
        self._anim_note = ""
        # 在途任务不断、也不强杀（解码跑了一半，强杀只会留半截状态）：
        # 留着引用让它自己跑完，靠 `_on_anim_ready` / `finished` 回收。
        # 真正"必须等它收工"的场景是控件销毁：那由 `shutdown_anim()` 负责。

    def shutdown_anim(self, timeout_ms: int = 5000) -> None:
        """停播 + 等在途解码收工。控件/窗口销毁前必须调用。

        不等的后果是闪退：`QThread` 在 `isRunning()` 时被销毁，
        Qt 直接 `abort()`（"QThread: Destroyed while thread is still
        running"），没有回溯、退出码 127。和 `ThumbCache.shutdown()`
        同一套思路（那边用的是 `waitForDone`），`MainWindow.closeEvent`
        里和它一起调。

        超时仍未结束的任务会被挪到模块级列表继续等（`_park_anim_task`），
        保证任何情况下都不会出现"带着运行中的线程被销毁"。
        可重复调用。
        """
        self._stop_anim()
        pending = [t for t in self._anim_tasks if t.isRunning()]
        deadline = time.monotonic() + timeout_ms / 1000.0
        for t in pending:
            left_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            t.wait(left_ms)
        for t in list(self._anim_tasks):
            if t.isRunning():
                _park_anim_task(t)
            self._anim_tasks.discard(t)

    def toggle_anim(self) -> None:
        """暂停 / 继续动图。静态图上是空操作。"""
        if len(self._anim_frames) < 2:
            return
        self._anim_paused = not self._anim_paused
        if self._anim_paused:
            self._timer.stop()
        else:
            self._schedule_next()
        self.update()

    # ------------------------------------------------------------ 缩放
    def _reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.zoomChanged.emit(self._zoom)

    @property
    def zoom(self) -> float:
        """当前显示倍率。1.0 = 适应窗口（不是"原始像素 1:1"）。

        这点必须讲清楚，否则用户按"100% 应该等于原图大小"去理解会困惑：
        本预览框先按可用区等比缩放到装得下，倍率是在那之上再乘的系数。
        """
        return self._zoom

    def set_zoom(self, value: float) -> None:
        """直接设到某个倍率（百分比输入框走这条）。越界由 `zoom_by` 夹取。

        用比值调 `zoom_by` 而不是直接给 `self._zoom` 赋值：
        锚点与平移的联动逻辑只有一份（在 `zoom_by` 里），
        输入框和滚轮的行为才不会各走各的：早先如果这里自己算一遍，
        输入框缩放到 400% 后图会停在左上角，而滚轮缩放是居中的。

        守卫必须写成 `not (value > 0)` 而不是 `value <= 0`：
        `nan <= 0` 是 False（NaN 的比较恒为 False），用后者会放 nan 进来，
        而 `min(ZOOM_MAX, 2.0 * nan)` 会把倍率直接顶到 16x。
        这个 bug 是测试 `test_set_zoom_ignores_nonpositive` 抓出来的。
        """
        if not (value > 0) or not (self._zoom > 0):
            return
        self.zoom_by(value / self._zoom)

    def reset_zoom(self) -> None:
        self._reset_view()               # 内含 zoomChanged 广播
        self.update()

    def zoom_by(self, factor: float, anchor: Optional[QPointF] = None) -> None:
        """按倍率缩放，`anchor` 是预览框内的锚点（默认正中）。

        锚点算法：设锚点在图内的相对位置比例不变。放大 k 倍时，
        让 `anchor` 落在图上的那个点保持在 `anchor` 屏幕位置。
        """
        old = self._zoom
        new = max(self.ZOOM_MIN, min(self.ZOOM_MAX, old * factor))
        if abs(new - old) < 1e-9:
            return                       # 已到顶/到底，别让 _pan 漂移
        r = self.rect().adjusted(1, 1, -1, -1).adjusted(10, 10, -10, -10)
        if anchor is None:
            anchor = QPointF(r.center())
        # 锚点相对画面中心的偏移，按倍率差值等比放大
        c = QPointF(r.center())
        self._pan = QPointF(
            anchor.x() - (anchor.x() - c.x() - self._pan.x()) * (new / old)
            - c.x() + (anchor.x() - c.x()),
            anchor.y() - (anchor.y() - c.y() - self._pan.y()) * (new / old)
            - c.y() + (anchor.y() - c.y()),
        )
        self._zoom = new
        self._clamp_pan()
        self.zoomChanged.emit(self._zoom)
        self.update()

    def _content_size(self) -> QSize:
        """当前倍率下图画出来的实际像素尺寸。"""
        if self._pixmap is None:
            return QSize(0, 0)
        r = self.rect().adjusted(1, 1, -1, -1).adjusted(10, 10, -10, -10)
        pm = self._pixmap.scaled(r.size(), Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
        return QSize(max(1, int(pm.width() * self._zoom)),
                     max(1, int(pm.height() * self._zoom)))

    def _clamp_pan(self) -> None:
        """把平移限制在"图边不越过可视区中线"的范围内，防止图被拖飞。"""
        c = self._content_size()
        avail = self.rect().adjusted(11, 11, -11, -11)
        mx = max(0.0, (c.width() - avail.width()) / 2.0)
        my = max(0.0, (c.height() - avail.height()) / 2.0)
        self._pan = QPointF(max(-mx, min(mx, self._pan.x())),
                            max(-my, min(my, self._pan.y())))

    def wheelEvent(self, event) -> None:
        """滚轮缩放。必须 accept，否则外层滚动区会把事件吃掉。"""
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(self.ZOOM_STEP if delta > 0 else 1.0 / self.ZOOM_STEP,
                         QPointF(event.position()))
            event.accept()
            return
        super().wheelEvent(event)

    # ------------------------------------------------------------ 平移
    def mousePressEvent(self, event) -> None:
        if (event.button() == Qt.LeftButton and self._zoom > 1.001
                and self._pixmap is not None):
            self._panning = True
            self._pan_start = QPointF(event.position())
            self._pan_origin = QPointF(self._pan)
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning:
            d = QPointF(event.position()) - self._pan_start
            self._pan = QPointF(self._pan_origin.x() + d.x(),
                                self._pan_origin.y() + d.y())
            self._clamp_pan()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._panning and event.button() == Qt.LeftButton:
            self._panning = False
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        k = event.key()
        if k in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_by(self.ZOOM_STEP)
        elif k in (Qt.Key_Minus, Qt.Key_Underscore):
            self.zoom_by(1.0 / self.ZOOM_STEP)
        elif k == Qt.Key_0:
            self.reset_zoom()
        elif k == Qt.Key_Space:
            self.toggle_anim()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(C_BORDER, 1))
        painter.setBrush(QBrush(C_PLACEHOLDER))
        painter.drawRoundedRect(r, 10, 10)

        if self._pixmap is not None:
            avail = r.adjusted(10, 10, -10, -10)
            # 等比缩放装进可用区（KeepAspectRatio），绝不拉伸。
            # 变形的大图会让人误判"这两张构图不一样"。
            pm = self._pixmap.scaled(avail.size(), Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
            if abs(self._zoom - 1.0) > 1e-6:
                pm = pm.scaled(max(1, int(pm.width() * self._zoom)),
                               max(1, int(pm.height() * self._zoom)),
                               Qt.IgnoreAspectRatio,
                               Qt.SmoothTransformation)
            x = avail.left() + (avail.width() - pm.width()) // 2 \
                + int(self._pan.x())
            y = avail.top() + (avail.height() - pm.height()) // 2 \
                + int(self._pan.y())
            # 放大后图会溢出可视区：裁掉，别糊到边框外面
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(r.adjusted(2, 2, -2, -2), 9, 9)
            painter.setClipPath(clip)
            painter.drawPixmap(x, y, pm)
            painter.restore()
            # 只在"动图"时提示，常态下的静图不打扰
            self._paint_badges(painter, r)
        elif self._path:
            painter.setPen(C_TEXT_DIM)
            painter.drawText(r, Qt.AlignCenter, "加载中…")
        else:
            painter.setPen(C_TEXT_DIM)
            painter.drawText(r, Qt.AlignCenter,
                             "点击左侧任意一张图片\n在这里预览大图")
        painter.end()

    def _paint_badges(self, painter: QPainter, r) -> None:
        """左下角浮标：动图状态。不占布局，不盖住图心。

        倍率不再画在这里：预览框正下方新增了百分比输入框
        （`window._build_zoom_row()`），它必须常驻显示当前倍率才可用。
        同一个数字在相隔 20px 的两处同时出现是明显的重复，
        所以这里只留动图信息（帧号 / 暂停 / 截断说明）。

        总宽度必须夹在预览框内。侧栏只有 260px 宽，而
        "帧数过多，只播前 300 帧（原 4800 帧）"这种说明很长 ——
        不夹的话浮标会横向溢出、被裁掉半截，看起来像渲染坏了。
        超宽时优先砍说明文字（保留帧号这个"操作反馈"）。
        """
        parts = []
        if len(self._anim_frames) > 1:
            state = "⏸" if self._anim_paused else "▶"
            parts.append(f"{state} {self._anim_idx + 1}/{len(self._anim_frames)}")
        if self._anim_note:
            parts.append(self._anim_note)
        if not parts:
            return
        f = QFont(painter.font())
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1.0))
        painter.setFont(f)
        fm = QFontMetrics(f)
        # 可用宽度：留出左右各 8px 的呼吸位
        max_w = max(40, r.width() - 16)
        # 先用全量文字，放不下就逐级丢：先丢说明，再截断说明
        txt = "  ·  ".join(parts)
        while parts and fm.horizontalAdvance(txt) + 14 > max_w:
            if len(parts) > 1:
                parts = parts[:-1]          # 先丢说明（最后加进来的）
            else:
                # 只剩一项还放不下 → 按字符砍，末尾加省略号
                t = parts[0]
                while t and fm.horizontalAdvance(t + "…") + 14 > max_w:
                    t = t[:-1]
                parts = [t + "…"]
            txt = "  ·  ".join(parts)
        w = min(max_w, fm.horizontalAdvance(txt) + 14)
        h = fm.height() + 6
        box = QRect(r.left() + 8, r.bottom() - h - 8, w, h)
        # 深色半透明底 + 浅字：不管图是亮是暗都读得清
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 138)))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(QColor("#F2F0E9"))
        painter.drawText(box.adjusted(0, 0, 0, 0), Qt.AlignCenter, txt)

    def closeEvent(self, event) -> None:
        # 窗口关了还在刷帧 → 白刷；更糟的是在途解码线程还没收工就被销毁，
        # Qt 会直接 abort（闪退）。所以这里要等，不只是停。
        # （窗口关闭时子控件不一定会收到 closeEvent，所以 `MainWindow.closeEvent`
        #   里也调了一次；`shutdown_anim` 可重复调用。）
        self.shutdown_anim()
        super().closeEvent(event)


class BrandText(QLabel):
    """品牌字：渐变文字，冷青 → 主色 → 暖琥珀。

    效果等价于下面这段 CSS：

    ```css
    .brand {
        background: linear-gradient(90deg, var(--accent-deep) 20%, var(--accent) 55%, var(--amber) 110%);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }
    ```

    它是整个界面上"最像流萤"的一处：那截琥珀收尾正好对上
    背景渐变的暖米收尾，一冷一暖互为呼应。别处不要再加渐变文字，
    工具软件里超过一处就会显得花。

    实现方式和 CSS 的 `background-clip: text` 等价：先用渐变填满整块，
    再用 `CompositionMode_SourceIn` 把文字像素以外的地方抹掉。
    """

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._a = QColor(C_BRAND_A)
        self._b = QColor(C_BRAND_B)
        self._c = QColor(C_BRAND_C)

    def set_colors(self, a: str, b: str, c: str) -> None:
        """就地改三段色（`apply_theme()` 调）。"""
        for color, hexval in ((self._a, a), (self._b, b), (self._c, c)):
            r, g, bl, _ = _rgba_local(hexval)
            color.setRgb(r, g, bl)

    def paintEvent(self, event) -> None:
        # 先用普通方式把文字画到一张透明底的图上
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setFont(self.font())
        p.setPen(QPen(QColor("#000000")))
        p.drawText(self.rect(), int(self.alignment()), self.text())
        p.end()

        # 再用渐变 → `SourceIn` 上色：只保留原文字的 alpha 形状。
        # 渐变要横向铺满整个控件宽度（CSS 的 `linear-gradient(90deg)`），
        # 用相对比例而不是控件坐标，否则字越长渐变越"陡"。
        grad = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        grad.setColorAt(0.0, self._a)
        grad.setColorAt(0.20, self._a)
        grad.setColorAt(0.55, self._b)
        grad.setColorAt(1.0, self._c)

        out = QPainter(self)
        out.drawPixmap(0, 0, pm)
        out.setCompositionMode(QPainter.CompositionMode_SourceIn)
        out.fillRect(self.rect(), QBrush(grad))
        out.end()


class PanelSurface(QWidget):
    """一块"面板"：竖直渐变底 + 圆角 + 描边 + 投影。

    这类面板在 CSS 里大概是这样：

    ```css
    .sidebar {
        background: linear-gradient(180deg, #E7F8F3, #FBFEFD 55%, #FDF6EA 130%);
        border: 1px solid var(--line); border-radius: 18px;
        box-shadow: 0 6px 24px rgba(23, 108, 96, 0.1);
    }
    ```

    为什么值得自绘：没有它时侧栏是"一片没有边界的空白"，
    和主画布糊在一起、层级全靠分隔线撑着。加上之后才有
    「面板浮在渐变背景上」的层次：整个界面因此
    "干净但有厚度"，而不是一片扁平的白。

    半径取 14（和卡片一致），不用 18 ——
    桌面原生窗口的圆角比网页克制一档才不显得"网页味"。
    """

    RADIUS = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self._c0 = QColor(C_PANEL_TOP)
        self._c1 = QColor(C_PANEL_MID)
        self._c2 = QColor(C_PANEL_END)
        self._line = QColor(C_PANEL_LINE)
        self._shadow = QColor(C_PANEL_SHADOW)
        self.shadow_on = True

    def set_stops(self, c0: str, c1: str, c2: str) -> None:
        for color, hexval in ((self._c0, c0), (self._c1, c1), (self._c2, c2)):
            r, g, b, _ = _rgba_local(hexval)
            color.setRgb(r, g, b)

    def set_line(self, hexval: str) -> None:
        r, g, b, _ = _rgba_local(hexval)
        self._line.setRgb(r, g, b)

    def set_shadow(self, rgb_triple: str) -> None:
        r, g, b = (int(v.strip()) for v in rgb_triple.split(","))
        self._shadow.setRgb(r, g, b)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, self.RADIUS, self.RADIUS)

        # ---- 投影：两层递减 alpha，只往下偏 ----
        if self.shadow_on and C_PANEL_SHADOW_ALPHA > 0:
            painter.setPen(Qt.NoPen)
            for i in (3, 1):
                a = int(C_PANEL_SHADOW_ALPHA * (0.40 ** (4 - i)))
                if a <= 0:
                    continue
                c = QColor(self._shadow)
                c.setAlpha(a)
                painter.setBrush(QBrush(c))
                sr = r.adjusted(-i, -i + 2, i, i + 2)
                painter.drawRoundedRect(sr, self.RADIUS + i, self.RADIUS + i)

        # ---- 面板本体：竖直渐变（180deg，冷→暖） ----
        grad = QLinearGradient(0.0, float(r.top()), 0.0, float(r.bottom()))
        # 三段色停在 0% / 55% / 130%：第三段落在控件之外，
        # 所以要按"可见比例"折算，否则暖米永远露不出来。
        grad.setColorAt(0.0, self._c0)
        grad.setColorAt(0.55, self._c1)
        grad.setColorAt(1.0, self._c2)
        painter.fillPath(path, QBrush(grad))

        painter.setPen(QPen(self._line, 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.end()


class GradientPage(QWidget):
    """带三段渐变背景的页面容器。

    这是"流萤"最显眼的一处视觉语言：
    `linear-gradient(155deg, #eafaf6, #f4fbf8, #fdf8ef)` ——
    从冷薄荷过渡到暖米。那个暖收尾是灵魂：萤火虫的光是暖的，
    纯冷绿会让整个软件看起来像医疗设备。

    QSS 给不了渐变背景（`background: linear-gradient(...)` 在 Qt 样式表里
    不生效），所以只能自绘。放在最底层、`paintEvent` 里画一次渐变，
    子控件靠 `QSS` 的 `transparent` 背景透出来。

    渐变角度的换算：CSS 的 `155deg` 是"从正上方顺时针转 155°"，
    而 `QLinearGradient` 用起止点坐标。155° 对应的方向向量约为
    `(sin155°, -cos155°) ≈ (0.42, 0.91)`，即"右下偏下"。
    这里用归一化后的相对坐标，保证不同窗口尺寸下观感一致。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        #: 三段颜色，由 `apply_theme()` 刷新（世界可变，别重新赋值）
        self._c0 = QColor("#EAFAF6")
        self._c1 = QColor("#F4FBF8")
        self._c2 = QColor("#FDF8EF")
        #: 是否启用渐变。关掉就退回纯色（供测试/无渐变需求时用）
        self.gradient_on = True

    def set_stops(self, c0: str, c1: str, c2: str) -> None:
        """就地改三段色：同样不能换对象，`paintEvent` 每帧读它们。"""
        for color, hexval in ((self._c0, c0), (self._c1, c1), (self._c2, c2)):
            r, g, b, _ = _rgba_local(hexval)
            color.setRgb(r, g, b)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        r = self.rect()
        if not self.gradient_on:
            painter.fillRect(r, self._c1)
            painter.end()
            return
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        # 155deg：起点略偏左上、终点在右下。用 rect 的相对比例而不是绝对值，
        # 这样窗口拉大时渐变不会"变陡"（绝对坐标会）。
        grad.setStart(r.left() + r.width() * 0.10, r.top() - r.height() * 0.06)
        grad.setFinalStop(r.left() + r.width() * 0.92,
                          r.top() + r.height() * 1.06)
        grad.setColorAt(0.0, self._c0)
        grad.setColorAt(0.45, self._c1)
        grad.setColorAt(1.0, self._c2)
        painter.fillRect(r, QBrush(grad))
        painter.end()


class BrandDot(QWidget):
    """品牌圆点：一颗带萤火光晕的主色圆点。

    整个界面上唯一给「流萤」角色露出的位置。别在别处再加光效 ——
    这是工具软件，不是展示页；光效用多了会让图片的颜色判断失真。
    """

    SIZE = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect()
        painter.setPen(Qt.NoPen)
        # 两层光晕（模拟 CSS 的双层 box-shadow）：外层更淡更大
        painter.setBrush(QBrush(glow(C_ACCENT, 1.0)))
        painter.drawEllipse(r.adjusted(-5, -5, 5, 5))
        painter.setBrush(QBrush(C_ACCENT))
        painter.drawEllipse(r.adjusted(3, 3, -3, -3))
        painter.end()


class StageProgress(QWidget):
    """多段进度条。

    刻意不做成"一根会停在 99% 的条"：每个阶段各占一段独立进度，
    收尾步骤（写库、统计、缩略图落盘）也各有自己的段，
    否则用户只能把"卡在 99%"理解成卡死。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stages: list[tuple[str, int, int]] = []
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(46)

    def set_stages(self, stages: list[tuple[str, int, int]]) -> None:
        self._stages = list(stages)
        self.update()

    def update_stage(self, name: str, done: int, total: int) -> None:
        for i, (n, _d, _t) in enumerate(self._stages):
            if n == name:
                self._stages[i] = (name, done, total)
                self.update()
                return
        self._stages.append((name, done, total))
        self.update()

    @property
    def stages(self) -> list[tuple[str, int, int]]:
        return list(self._stages)

    def overall(self) -> float:
        if not self._stages:
            return 0.0
        done = sum(min(d, t) for _n, d, t in self._stages)
        total = sum(max(t, 1) for _n, _d, t in self._stages)
        return done / total if total else 0.0

    def paintEvent(self, event) -> None:
        if not self._stages:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        n = len(self._stages)
        # 每段宽度 = 标签 + 进度条 + 百分比，三者必须一起算进去。
        # 实测踩过：只按「标签 + 条」算段宽，结果百分比压到了下一段的标签上
        # （截图看出来的，纯逻辑测不出来）。
        label_w = 68
        pct_w = 36
        gap = 12
        bar_h = 6
        row_h = 18
        usable = max(160, self.width() - 8)
        seg_w = max(label_w + 24 + pct_w, (usable - (n - 1) * gap) // n)
        bar_w = max(24, seg_w - label_w - pct_w)

        for i, (name, done, total) in enumerate(self._stages):
            x = 4 + i * (seg_w + gap)
            y = (self.height() - row_h) // 2
            painter.setPen(C_TEXT_DIM)
            painter.drawText(QRect(x, y, label_w, row_h),
                             Qt.AlignVCenter | Qt.AlignLeft, name)

            bx = x + label_w
            track = QRect(bx, y + (row_h - bar_h) // 2, bar_w, bar_h)
            painter.fillRect(track, QBrush(C_BORDER))
            frac = 0.0 if total <= 0 else min(1.0, done / total)
            if frac > 0:
                fill = QRect(track.left(), track.top(),
                             max(2, int(track.width() * frac)), bar_h)
                if frac >= 1.0:
                    painter.fillRect(fill, QBrush(C_KEEP))
                else:
                    #: 行进段的填充用横向渐变（深主色 → 主色 → 暖琥珀），
                    #: 写成 CSS 是 `linear-gradient(90deg, accent-deep 20%, accent 55%, amber 110%)`。
                    #: 纯色条是"进度条"，渐变条才是"萤火在往前走"。
                    #: 而且那截琥珀收尾和品牌字、背景暖收尾是同一个语言。
                    #:
                    #: 渐变要铺在整条 track 的坐标系上、只显示 `fill` 那一段，
                    #: 不能按 `fill` 自己的宽度铺，否则进度每往前走一点、
                    #: 渐变的色序就整体重算一遍，看起来像"颜色在抖"。
                    g = QLinearGradient(float(track.left()), 0.0,
                                        float(track.right()), 0.0)
                    g.setColorAt(0.0, C_ACCENT_DEEP)
                    g.setColorAt(0.55, C_ACCENT)
                    g.setColorAt(1.0, C_EMBER)
                    painter.fillRect(fill, QBrush(g))
                # 萤火光点：只挂在行进端，不铺满整条：铺满会让进度条
                # 变成一块发光色带，喧宾夺主。这里是三处允许用光效的地方之一。
                if 0.0 < frac < 1.0:
                    gr = 14
                    gx = fill.right() - gr / 2
                    gy = fill.center().y() - gr / 2
                    painter.setBrush(QBrush(glow(C_ACCENT, 0.9)))
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(QRect(int(gx), int(gy), gr, gr))

            painter.setPen(C_TEXT_DIM)
            # total <= 0 表示「总数未定」（例如遍历阶段只能报已发现数），
            # 这时显示计数而不是百分比，避免假装有个进度。
            txt = f"{done:,}" if total <= 0 else f"{frac * 100:.0f}%"
            painter.drawText(QRect(track.right() + 4, y, pct_w, row_h),
                             Qt.AlignVCenter | Qt.AlignLeft, txt)
        painter.end()
