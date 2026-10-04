"""自绘控件，以及主题色在本模块内的常量落地。

`theme.apply_theme()` 会先就地刷新这里的 `C_*` 常量、再刷自绘控件，所以 paint 里
统一读这些模块级常量，不要现取主题。

本模块不 import theme（theme 反过来要用这里的 delegate），所以颜色工具函数
在这里留了一份本地实现。"""

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

CARD_PAD = 12
LABEL_H = 22
ITEM_GAP = 10
CAPTION_H = 16
THUMB_GAP = 6
MIN_CARD_H = CARD_PAD * 2 + LABEL_H + 6 + THUMB_SIZE + CAPTION_H

C_CARD = QColor("#FFFFFF")
C_BORDER = QColor("#E3E3E0")
C_TEXT = QColor("#2C2C2A")
C_TEXT_DIM = QColor("#888780")
C_KEEP = QColor("#1D9E75")
C_DEL = QColor("#E24B4A")
C_UNDECIDED = QColor("#D3D1C7")
C_PLACEHOLDER = QColor("#F1EFE8")

C_FOCUS = QColor("#185FA5")
C_HOVER = QColor("#8FBCE8")
C_CARD_FOCUS = QColor("#B9D3EE")
C_FOCUS_WASH = QColor(24, 95, 165, 26)

C_TIP_BG = QColor("#FFFFFF")
C_TIP_BORDER = QColor("#D9ECE6")
C_TIP_TEXT = QColor("#1D2B28")

C_ACCENT = QColor("#14B8A6")

C_ACCENT_DEEP = QColor("#0D9488")

C_EMBER = QColor("#D97706")

C_SHADOW = QColor(23, 108, 96)

C_SHADOW_ALPHA = 15

C_PANEL_TOP = QColor("#E7F8F3")
C_PANEL_MID = QColor("#FBFEFD")
C_PANEL_END = QColor("#FDF6EA")

C_PANEL_LINE = QColor("#D9ECE6")

C_PANEL_SHADOW = QColor(23, 108, 96)
C_PANEL_SHADOW_ALPHA = 26

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
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def set_accent(hex_color: str) -> None:
    c = QColor(hex_color)
    C_ACCENT.setRgb(c.red(), c.green(), c.blue())


def glow(color: QColor, strength: float = 1.0) -> QRadialGradient:
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
    h = int(hashlib.md5(path.encode("utf-8", "replace")).hexdigest()[:6], 16)
    hue = h % 360
    return QColor.fromHsv(hue, 40, 238)


class GroupDelegate(QStyledItemDelegate):

    def __init__(self, model: GroupModel, thumbs: ThumbCache, view=None):
        super().__init__(view)
        self.model = model
        self.thumbs = thumbs
        self.view = view
        self.view_width = 900
        self.thumb_size: int = THUMB_SIZE
        self.sel_set: set[tuple[int, int]] = set()
        self.anchor: Optional[tuple[int, int]] = None
        self.hover: Optional[tuple[int, int]] = None
        self._font_label = QFont()
        self._font_label.setPointSize(9)
        self._font_label.setBold(True)
        self._font_small = QFont()
        self._font_small.setPointSize(8)

    def set_selected(self, vrow: int, pos: int) -> None:
        self.sel_set = {(vrow, pos)}
        self.anchor = (vrow, pos)

    def set_selection(self, hits: set[tuple[int, int]],
                      anchor: Optional[tuple[int, int]] = None) -> None:
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
        return self.anchor == (vrow, pos)

    @property
    def sel_vrow(self) -> int:
        return self.anchor[0] if self.anchor else -1

    @property
    def sel_pos(self) -> int:
        return self.anchor[1] if self.anchor else -1

    def set_thumb_size(self, size: int) -> bool:
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

    def _item_rect(self, card_rect: QRect, pos: int) -> QRect:
        per = self.per_line()
        x0 = card_rect.left() + CARD_PAD
        line, col = divmod(pos, per)
        x = x0 + col * (self.thumb_size + ITEM_GAP)
        y = (card_rect.top() + CARD_PAD + LABEL_H + 6
             + line * (self.thumb_size + THUMB_GAP + CAPTION_H + 4))
        return QRect(x, y, self.thumb_size, self.thumb_size)

    def hit_test(self, viewport_pos) -> Optional[tuple[int, int]]:
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
        vr = view.visualRect(idx)
        rel = viewport_pos - vr.topLeft()
        card = QRect(0, 0, vr.width(), vr.height())
        for pos in range(row.count):
            if self._item_rect(card, pos).contains(rel):
                return (vrow, pos)
        return None

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
        self._draw_card_shadow(painter, rect)

        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        painter.fillPath(path, QBrush(C_CARD))
        if row.oversized:
            pen = QPen(QColor("#E24B4A"), 1.4)
            pen.setStyle(Qt.DashLine)
        elif focus:
            pen = QPen(C_CARD_FOCUS, 1.6)
        else:
            pen = QPen(C_BORDER, 1.0)
        painter.setPen(pen)
        painter.drawPath(path)

    def _draw_card_shadow(self, painter: QPainter, rect: QRect) -> None:
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

        bg, fg = BADGE.get(row.kind, BADGE["perceptual"])
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
        if row.label and row.label != row.kind:
            return row.label
        if row.kind == "exact":
            return "字节完全相同"
        if row.kind == "semantic":
            return "语义相似"
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
            painter.fillRect(box, QBrush(placeholder_color(item.path)))
            self.thumbs.request(item.path)
        else:
            painter.drawPixmap(box, pix.scaled(box.size(), Qt.KeepAspectRatioByExpanding,
                                               Qt.SmoothTransformation))

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
            cap = f"第 {index + 1}/{total} 张"
            color = C_FOCUS
        painter.setFont(self._font_small)
        painter.setPen(color)
        painter.drawText(QRect(x, y + self.thumb_size + 2, self.thumb_size, CAPTION_H),
                         Qt.AlignHCenter | Qt.AlignTop, cap)

    @staticmethod
    def _draw_selection_ring(painter: QPainter, box: QRect,
                             anchor: bool = True) -> None:
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




class ComboItemDelegate(QStyledItemDelegate):

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        r = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

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
            painter.drawRoundedRect(r.adjusted(2, 1, -2, -1), 6, 6)
            if selected:
                painter.setPen(QPen(_DELEGATE_SEL_LINE, 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(r.adjusted(2, 1, -2, -1), 6, 6)

        painter.setPen(_DELEGATE_SEL_TEXT if selected else _DELEGATE_TEXT)
        font = painter.font()
        font.setBold(selected)
        painter.setFont(font)
        painter.drawText(
            r.adjusted(12, 0, -8, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
        painter.restore()


_DELEGATE_SEL_BG = QColor("#DCF5EF")
_DELEGATE_SEL_TEXT = QColor("#0D9488")
_DELEGATE_SEL_LINE = QColor("#14B8A6")
_DELEGATE_HOVER_BG = QColor("#F6FBF9")
_DELEGATE_TEXT = QColor("#1D2B28")



class InstantToolTip(QWidget):

    OFFSET_Y = 6
    SCREEN_MARGIN = 6
    MAX_W = 380
    PAD_X = 11
    PAD_Y = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint
                            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._owner: Optional[QWidget] = None
        self._provider = None
        self._lines: list = []
        self.hide()

    def attach(self, widget: QWidget, text) -> None:
        self._provider = text
        if not hasattr(widget, "_instant_tooltips"):
            widget._instant_tooltips = []
        widget._instant_tooltips.append(self)
        widget.installEventFilter(_InstantToolTipFilter(self))
        widget.destroyed.connect(self._on_owner_destroyed)

    def _on_owner_destroyed(self, *_a) -> None:
        self._owner = None
        self.hide()

    def _current_text(self) -> str:
        t = self._provider
        if t is None:
            return ""
        try:
            t = t() if callable(t) else t
        except Exception:
            return ""
        return str(t or "").strip()

    def popup_for(self, widget: QWidget) -> None:
        txt = self._current_text()
        if not txt:
            self.hide()
            return
        self._owner = widget
        self._relayout(txt)
        self._place(widget)
        self.show()
        self.raise_()

    def dismiss(self, widget: Optional[QWidget] = None) -> None:
        if widget is not None and self._owner is not widget:
            return
        self._owner = None
        self.hide()

    def _relayout(self, txt: str) -> None:
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
        try:
            anchor = widget.mapToGlobal(QPoint(0, widget.height()))
        except Exception:
            anchor = widget.pos()
        x = anchor.x()
        y = anchor.y() + self.OFFSET_Y

        scr = widget.screen().availableGeometry() if widget.screen() else None
        if scr is not None:
            if x + self.width() > scr.right() - self.SCREEN_MARGIN:
                x = scr.right() - self.SCREEN_MARGIN - self.width()
            if x < scr.left() + self.SCREEN_MARGIN:
                x = scr.left() + self.SCREEN_MARGIN
            if y + self.height() > scr.bottom() - self.SCREEN_MARGIN:
                y = widget.mapToGlobal(QPoint(0, 0)).y() \
                    - self.height() - self.OFFSET_Y
        self.move(int(x), int(y))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
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
            if obj.hasFocus():
                self.tip.popup_for(obj)
        elif et in (QEvent.FocusOut, QEvent.Hide):
            self.tip.dismiss(obj)
        return False



_ANIM_TOKEN = itertools.count(1)


def _next_anim_token() -> int:
    return next(_ANIM_TOKEN)


_ANIM_PARKED: list = []


def _park_anim_task(task) -> None:
    if task in _ANIM_PARKED:
        return
    _ANIM_PARKED.append(task)

    def _release(t=task) -> None:
        try:
            t.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            _ANIM_PARKED.remove(t)
        except (ValueError, RuntimeError):
            pass

    task.finished.connect(_release)
    if not task.isRunning():
        _release()


def _retire_anim_task(task) -> None:
    if task.isRunning():
        _park_anim_task(task)


def _pixmap_from_rgb(arr) -> QPixmap:
    h, w, ch = arr.shape
    img = QImage(arr.data, w, h, ch * w, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(img)


class _AnimDecodeTask(QThread):

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
        except Exception:
            anim = None
        self.done.emit(self.token, anim)


class PreviewPane(QWidget):

    ZOOM_MIN = 0.2
    ZOOM_MAX = 16.0
    ZOOM_STEP = 1.15
    STILL_SIDE = 1024

    zoomChanged = Signal(float)

    def __init__(self, cache: "ThumbCache", parent=None):
        super().__init__(parent)
        self.cache = cache
        self._path: Optional[str] = None
        self._pixmap: Optional[QPixmap] = None
        self._token: Optional[int] = None
        self._caption: str = ""
        self._sub: str = ""
        self._zoom: float = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._panning = False
        self._pan_start = QPointF(0.0, 0.0)
        self._pan_origin = QPointF(0.0, 0.0)
        self._anim_frames: list = []
        self._anim_delays: list = []
        self._anim_idx: int = 0
        self._anim_token: Optional[int] = None
        self._anim_note: str = ""
        self._anim_paused: bool = False
        self._anim_tasks: set = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._next_frame)
        self.setMinimumSize(150, 150)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        cache.loaded.connect(self._on_loaded)

    def show_path(self, path: Optional[str], caption: str = "",
                  sub: str = "") -> None:
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
            return
        self._token = None
        self._pixmap = self.cache.get(self._path or "")
        self.update()

    def _start_anim_if_needed(self, path: str) -> bool:
        try:
            from imgdedup_core import decode as D
        except Exception:
            return False
        if not D.is_animated(path):
            return False
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
        self._anim_tasks.add(task)
        task.start()
        return True

    def _on_anim_ready(self, token: int, anim) -> None:
        stale = [t for t in self._anim_tasks if t.token == token]
        self._anim_tasks = {t for t in self._anim_tasks if t.token != token}
        for t in stale:
            _retire_anim_task(t)
        if token != self._anim_token:
            return
        self._anim_token = None
        if anim is None or not getattr(anim, "frames", None):
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
        self._timer.stop()
        self._anim_token = None
        self._anim_frames = []
        self._anim_delays = []
        self._anim_idx = 0
        self._anim_note = ""

    def shutdown_anim(self, timeout_ms: int = 5000) -> None:
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
        if len(self._anim_frames) < 2:
            return
        self._anim_paused = not self._anim_paused
        if self._anim_paused:
            self._timer.stop()
        else:
            self._schedule_next()
        self.update()

    def _reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.zoomChanged.emit(self._zoom)

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, value: float) -> None:
        if not (value > 0) or not (self._zoom > 0):
            return
        self.zoom_by(value / self._zoom)

    def reset_zoom(self) -> None:
        self._reset_view()
        self.update()

    def zoom_by(self, factor: float, anchor: Optional[QPointF] = None) -> None:
        old = self._zoom
        new = max(self.ZOOM_MIN, min(self.ZOOM_MAX, old * factor))
        if abs(new - old) < 1e-9:
            return
        r = self.rect().adjusted(1, 1, -1, -1).adjusted(10, 10, -10, -10)
        if anchor is None:
            anchor = QPointF(r.center())
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
        if self._pixmap is None:
            return QSize(0, 0)
        r = self.rect().adjusted(1, 1, -1, -1).adjusted(10, 10, -10, -10)
        pm = self._pixmap.scaled(r.size(), Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
        return QSize(max(1, int(pm.width() * self._zoom)),
                     max(1, int(pm.height() * self._zoom)))

    def _clamp_pan(self) -> None:
        c = self._content_size()
        avail = self.rect().adjusted(11, 11, -11, -11)
        mx = max(0.0, (c.width() - avail.width()) / 2.0)
        my = max(0.0, (c.height() - avail.height()) / 2.0)
        self._pan = QPointF(max(-mx, min(mx, self._pan.x())),
                            max(-my, min(my, self._pan.y())))

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(self.ZOOM_STEP if delta > 0 else 1.0 / self.ZOOM_STEP,
                         QPointF(event.position()))
            event.accept()
            return
        super().wheelEvent(event)

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

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(C_BORDER, 1))
        painter.setBrush(QBrush(C_PLACEHOLDER))
        painter.drawRoundedRect(r, 10, 10)

        if self._pixmap is not None:
            avail = r.adjusted(10, 10, -10, -10)
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
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(r.adjusted(2, 2, -2, -2), 9, 9)
            painter.setClipPath(clip)
            painter.drawPixmap(x, y, pm)
            painter.restore()
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
        max_w = max(40, r.width() - 16)
        txt = "  ·  ".join(parts)
        while parts and fm.horizontalAdvance(txt) + 14 > max_w:
            if len(parts) > 1:
                parts = parts[:-1]
            else:
                t = parts[0]
                while t and fm.horizontalAdvance(t + "…") + 14 > max_w:
                    t = t[:-1]
                parts = [t + "…"]
            txt = "  ·  ".join(parts)
        w = min(max_w, fm.horizontalAdvance(txt) + 14)
        h = fm.height() + 6
        box = QRect(r.left() + 8, r.bottom() - h - 8, w, h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 138)))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(QColor("#F2F0E9"))
        painter.drawText(box.adjusted(0, 0, 0, 0), Qt.AlignCenter, txt)

    def closeEvent(self, event) -> None:
        self.shutdown_anim()
        super().closeEvent(event)


class BrandText(QLabel):

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._a = QColor(C_BRAND_A)
        self._b = QColor(C_BRAND_B)
        self._c = QColor(C_BRAND_C)

    def set_colors(self, a: str, b: str, c: str) -> None:
        for color, hexval in ((self._a, a), (self._b, b), (self._c, c)):
            r, g, bl, _ = _rgba_local(hexval)
            color.setRgb(r, g, bl)

    def paintEvent(self, event) -> None:
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setFont(self.font())
        p.setPen(QPen(QColor("#000000")))
        p.drawText(self.rect(), int(self.alignment()), self.text())
        p.end()

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

        grad = QLinearGradient(0.0, float(r.top()), 0.0, float(r.bottom()))
        grad.setColorAt(0.0, self._c0)
        grad.setColorAt(0.55, self._c1)
        grad.setColorAt(1.0, self._c2)
        painter.fillPath(path, QBrush(grad))

        painter.setPen(QPen(self._line, 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.end()


class GradientPage(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._c0 = QColor("#EAFAF6")
        self._c1 = QColor("#F4FBF8")
        self._c2 = QColor("#FDF8EF")
        self.gradient_on = True

    def set_stops(self, c0: str, c1: str, c2: str) -> None:
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
        grad.setStart(r.left() + r.width() * 0.10, r.top() - r.height() * 0.06)
        grad.setFinalStop(r.left() + r.width() * 0.92,
                          r.top() + r.height() * 1.06)
        grad.setColorAt(0.0, self._c0)
        grad.setColorAt(0.45, self._c1)
        grad.setColorAt(1.0, self._c2)
        painter.fillRect(r, QBrush(grad))
        painter.end()


class BrandDot(QWidget):

    SIZE = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(glow(C_ACCENT, 1.0)))
        painter.drawEllipse(r.adjusted(-5, -5, 5, 5))
        painter.setBrush(QBrush(C_ACCENT))
        painter.drawEllipse(r.adjusted(3, 3, -3, -3))
        painter.end()


class StageProgress(QWidget):

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

    def paintEvent(self, event) -> None:
        if not self._stages:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        n = len(self._stages)
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
                    g = QLinearGradient(float(track.left()), 0.0,
                                        float(track.right()), 0.0)
                    g.setColorAt(0.0, C_ACCENT_DEEP)
                    g.setColorAt(0.55, C_ACCENT)
                    g.setColorAt(1.0, C_EMBER)
                    painter.fillRect(fill, QBrush(g))
                if 0.0 < frac < 1.0:
                    gr = 14
                    gx = fill.right() - gr / 2
                    gy = fill.center().y() - gr / 2
                    painter.setBrush(QBrush(glow(C_ACCENT, 0.9)))
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(QRect(int(gx), int(gy), gr, gr))

            painter.setPen(C_TEXT_DIM)
            txt = f"{done:,}" if total <= 0 else f"{frac * 100:.0f}%"
            painter.drawText(QRect(track.right() + 4, y, pct_w, row_h),
                             Qt.AlignVCenter | Qt.AlignLeft, txt)
        painter.end()
