"""主窗口：三栏布局、筛选、选中、右键菜单，以及删除的汇合点。

所有删除入口（工具栏、右键、键盘）最终都汇到 `_delete_paths_now`，由它统一做
「确认 → 执行 → 写 manifest → 更新撤销状态」。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox,
                               QPushButton, QSlider, QSplitter,
                               QVBoxLayout, QWidget)

from . import theme
from .dialogs import ask_delete
from .model import (DELETE, UNDECIDED, GroupModel, ThumbCache,
                    make_list_model)
from .theme import apply_theme, save_theme_key
from .widgets import (GradientPage, GroupDelegate, InstantToolTip,
                      PanelSurface, PreviewPane, StageProgress)

PREVIEW_SIZE = PreviewPane.STILL_SIDE
from .worker import MODES, MODE_ORDER, ScanWorker, mode_at, mode_info

KIND_LABEL = {
    "exact": "精确重复",
    "perceptual": "近似重复",
    "semantic": "语义相似",
    "cluster": "聚类（过合并）",
}

ALL_KINDS = "__all__"

THUMB_MIN, THUMB_MAX, THUMB_DEFAULT = 64, 256, 112

SIM_DEFAULT_PCT = 97

WHEEL_STEP_PX = 52

WHEEL_NOTCH = 120


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(base) / "ImgDedup"
    d.mkdir(parents=True, exist_ok=True)
    return d


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ImgDedup")
        self.resize(1360, 860)

        self.model = GroupModel(self)
        self.thumbs = ThumbCache(parent=self)
        self.delegate: Optional[GroupDelegate] = None

        self.worker: Optional[ScanWorker] = None
        self.root: str = ""
        self.meta: dict = {}
        self.last_manifest: str = ""
        self._catalog = None
        self._hover_row = None
        self._drag_anchor: Optional[tuple[int, int]] = None
        self._syncing: bool = False
        self.theme_key: str = theme.load_theme_key()

        self._build_ui()
        self._wire()
        self._sync_gradient()
        from .theme import _apply_combo_style
        _apply_combo_style(QApplication.instance(), theme.theme(self.theme_key))

    def _build_ui(self) -> None:
        self.page = GradientPage(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.page)

        lay = QVBoxLayout(self.page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lay.addWidget(self._build_toolbar())

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_center())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([190, 820, 350])
        lay.addWidget(split, 1)

        lay.addWidget(self._build_footer())
        self.main_split = split

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("toolbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)

        lay.addWidget(QLabel("扫描源"))
        self.ed_root = QLineEdit()
        self.ed_root.setReadOnly(True)
        self.ed_root.setPlaceholderText("选择一个包含图片的目录…")
        self.ed_root.setMinimumWidth(260)
        lay.addWidget(self.ed_root, 1)

        self.btn_pick = QPushButton("浏览…")
        lay.addWidget(self.btn_pick)

        lay.addSpacing(8)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems([MODES[k]["name"] for k in MODE_ORDER])
        lay.addWidget(self.cmb_mode)

        self.lbl_mode = QLabel()
        self.lbl_mode.setObjectName("modeHint")
        self.lbl_mode.setWordWrap(False)
        self.lbl_mode.setTextInteractionFlags(Qt.NoTextInteraction)
        lay.addWidget(self.lbl_mode, 1)
        self._last_mode_idx = 0
        self.cmb_mode.activated.connect(self._guard_deep_mode)
        self.cmb_mode.currentIndexChanged.connect(self._sync_mode_hint)
        self._sync_mode_hint()

        self.tip_mode = InstantToolTip(self)
        self.tip_mode.attach(self.cmb_mode, self._mode_tip_text)
        self.tip_mode.attach(self.lbl_mode, self._mode_tip_text)

        lay.addWidget(QLabel("相似度"))
        self.sld = QSlider(Qt.Horizontal)
        self.sld.setRange(70, 99)
        self.sld.setValue(SIM_DEFAULT_PCT)
        self.sld.setFixedWidth(96)
        self.lbl_sld = QLabel(f"{SIM_DEFAULT_PCT / 100:.2f}")
        self.lbl_sld.setFixedWidth(34)
        lay.addWidget(self.sld)
        lay.addWidget(self.lbl_sld)

        lay.addStretch(1)
        self.btn_start = QPushButton("开始扫描")
        self.btn_start.setEnabled(False)
        lay.addWidget(self.btn_start)

        self.cmb_theme = QComboBox()
        for key, t in theme.THEMES.items():
            self.cmb_theme.addItem(t["name"], key)
        idx = self.cmb_theme.findData(self.theme_key)
        if idx >= 0:
            self.cmb_theme.setCurrentIndex(idx)
        self.cmb_theme.setFixedWidth(120)
        lay.addWidget(self.cmb_theme)
        return bar

    def _build_left(self) -> QWidget:
        outer = QWidget()
        olay = QVBoxLayout(outer)
        olay.setContentsMargins(8, 8, 4, 8)
        olay.setSpacing(0)

        self.sidebar_panel = PanelSurface()
        olay.addWidget(self.sidebar_panel)

        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 10, 8, 10)
        lay.setSpacing(6)
        pl = QVBoxLayout(self.sidebar_panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(box)

        lbl = QLabel("分组筛选")
        lbl.setObjectName("sectionLabel")
        lay.addWidget(lbl)

        self.filter_list = QListWidget()
        self.filter_list.setFrameShape(QFrame.NoFrame)
        self.filter_list.setSelectionMode(QAbstractItemView.SingleSelection)
        lay.addWidget(self.filter_list, 1)

        self.tip_filter = InstantToolTip(self)
        self.tip_filter.attach(self.filter_list, self._filter_tip_text)

        self.lbl_filter_state = QLabel("")
        self.lbl_filter_state.setStyleSheet("color:#B4B2A9;")
        lay.addWidget(self.lbl_filter_state)

        lbl2 = QLabel("保护规则")
        lbl2.setObjectName("sectionLabel")
        lbl2.setContentsMargins(0, 8, 0, 0)
        lay.addWidget(lbl2)
        self.chk_keep_one = QCheckBox("每组至少保留 1 张")
        self.chk_keep_one.setChecked(True)
        self.chk_keep_one.setEnabled(False)
        self.chk_hardlink = QCheckBox("硬链接保护")
        self.chk_hardlink.setChecked(True)
        self.chk_hardlink.setEnabled(False)
        self.chk_oversized = QCheckBox("超大聚类不参与批量删除")
        self.chk_oversized.setChecked(True)
        self.chk_oversized.setEnabled(False)
        for c in (self.chk_keep_one, self.chk_hardlink, self.chk_oversized):
            lay.addWidget(c)

        lbl3 = QLabel("缩略图大小")
        lbl3.setObjectName("sectionLabel")
        lbl3.setContentsMargins(0, 10, 0, 0)
        lay.addWidget(lbl3)

        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        self.sld_thumb = QSlider(Qt.Horizontal)
        self.sld_thumb.setRange(THUMB_MIN, THUMB_MAX)
        self.sld_thumb.setValue(THUMB_DEFAULT)
        size_row.addWidget(self.sld_thumb, 1)
        self.lbl_thumb = QLabel(f"{THUMB_DEFAULT}")
        self.lbl_thumb.setFixedWidth(30)
        self.lbl_thumb.setStyleSheet("color:#6F8A83;")
        size_row.addWidget(self.lbl_thumb)
        lay.addLayout(size_row)
        self.tip_thumb = InstantToolTip(self)
        self.tip_thumb.attach(self.sld_thumb, "拖动调整缩略图大小（64–256 px）")
        return outer


    def _build_center(self) -> QWidget:
        self.view = QListView()
        self.view.setModel(make_list_model(self.model))
        self.delegate = GroupDelegate(self.model, self.thumbs, self.view)
        self.view.setItemDelegate(self.delegate)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setUniformItemSizes(False)
        self.view.setSpacing(0)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.verticalScrollBar().setSingleStep(WHEEL_STEP_PX)
        self.view.viewport().installEventFilter(self)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().setCursor(Qt.PointingHandCursor)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        return self.view

    def _build_right(self) -> QWidget:
        self.preview_cache = ThumbCache(size=PREVIEW_SIZE, capacity=12,
                                        parent=self)
        self.preview = PreviewPane(self.preview_cache)

        pbox = QWidget()
        play = QVBoxLayout(pbox)
        play.setContentsMargins(0, 0, 0, 0)
        play.setSpacing(6)
        play.addWidget(self.preview, 1)
        play.addLayout(self._build_zoom_row())

        info = QWidget()
        lay = QVBoxLayout(info)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)

        lbl = QLabel("当前选中")
        lbl.setStyleSheet("color:#888780;")
        lay.addWidget(lbl)

        self.lbl_title = QLabel("—")
        self.lbl_title.setWordWrap(True)
        f = self.lbl_title.font()
        f.setBold(True)
        self.lbl_title.setFont(f)
        lay.addWidget(self.lbl_title)

        self.lbl_focus = QLabel("")
        self.lbl_focus.setWordWrap(True)
        self.lbl_focus.setStyleSheet("color:#185FA5;")
        lay.addWidget(self.lbl_focus)

        self.lbl_detail = QLabel("")
        self.lbl_detail.setWordWrap(True)
        self.lbl_detail.setStyleSheet("color:#5F5E5A;")
        lay.addWidget(self.lbl_detail)

        self.lbl_reason = QLabel("")
        self.lbl_reason.setWordWrap(True)
        self.lbl_reason.setStyleSheet("color:#888780;")
        lay.addWidget(self.lbl_reason)

        lay.addStretch(1)

        self.btn_keep = QPushButton("设为保留")
        self.btn_mark = QPushButton("标记删除")
        self.btn_keep.setEnabled(False)
        self.btn_mark.setEnabled(False)
        lay.addWidget(self.btn_keep)
        lay.addWidget(self.btn_mark)

        self.btn_delete_one = QPushButton("删除选中的这一张…")
        self.btn_delete_one.setEnabled(False)
        lay.addWidget(self.btn_delete_one)

        pair = QHBoxLayout()
        pair.setSpacing(6)
        self.btn_apply_all = QPushButton("全部按推荐裁决")
        self.btn_undo = QPushButton("撤销所有裁决")
        pair.addWidget(self.btn_apply_all)
        pair.addWidget(self.btn_undo)
        lay.addLayout(pair)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(pbox)

        vsplit.addWidget(info)

        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        info.setMinimumHeight(300)
        vsplit.setSizes([434, 300])
        vsplit.setChildrenCollapsible(False)
        for i in range(vsplit.count()):
            vsplit.setCollapsible(i, False)
        self.right_split = vsplit

        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 10, 12, 10)
        outer.setSpacing(0)
        outer.addWidget(vsplit)
        return box

    def _build_zoom_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)

        lbl = QLabel("缩放")
        lbl.setStyleSheet("color:#888780;")
        row.addWidget(lbl)

        self.zoom_edit = QLineEdit("100%")
        self.zoom_edit.setFixedWidth(68)
        self.zoom_edit.setAlignment(Qt.AlignCenter)
        row.addWidget(self.zoom_edit)

        self.btn_zoom_reset = QPushButton("重置")
        self.btn_zoom_reset.setFixedWidth(52)
        row.addWidget(self.btn_zoom_reset)

        row.addStretch(1)

        self.zoom_edit.editingFinished.connect(self._apply_zoom_from_edit)
        self.btn_zoom_reset.clicked.connect(self.preview.reset_zoom)
        self.preview.zoomChanged.connect(self._sync_zoom_edit)

        self.tip_zoom = InstantToolTip(self)
        self.tip_zoom.attach(self.zoom_edit,
                             "输入百分比后回车缩放（100% = 适应窗口大小）")
        self.tip_zoom.attach(self.btn_zoom_reset,
                             "恢复到 100%（适应窗口大小）")
        return row

    def _sync_zoom_edit(self, zoom: float, force: bool = False) -> None:
        if not force and self.zoom_edit.isModified():
            return
        self.zoom_edit.setText(f"{zoom:.0%}")

    def _apply_zoom_from_edit(self) -> None:
        raw = self.zoom_edit.text().strip()
        for suffix in ("%", "％"):
            raw = raw.rstrip(suffix)
        try:
            pct = float(raw.strip())
        except ValueError:
            pct = float("nan")
        if not (pct > 0):
            self._sync_zoom_edit(self.preview.zoom, force=True)
            return
        self.preview.set_zoom(pct / 100.0)
        self._sync_zoom_edit(self.preview.zoom, force=True)

    def _build_footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("footer")
        lay = QVBoxLayout(foot)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(6)

        self.progress = StageProgress()
        lay.addWidget(self.progress)

        row = QHBoxLayout()
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color:#888780;")
        row.addWidget(self.lbl_status, 1)

        self.lbl_counts = QLabel("")
        row.addWidget(self.lbl_counts)

        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setEnabled(False)
        row.addWidget(self.btn_pause)

        self.btn_export = QPushButton("导出报告…")
        self.btn_export.setEnabled(False)
        row.addWidget(self.btn_export)

        self.btn_restore = QPushButton("撤销上次删除")
        self.btn_restore.setEnabled(False)
        row.addWidget(self.btn_restore)

        self.btn_delete = QPushButton("执行删除…")
        self.btn_delete.setEnabled(False)
        row.addWidget(self.btn_delete)
        lay.addLayout(row)
        return foot

    def _wire(self) -> None:
        self.btn_pick.clicked.connect(self.on_pick)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_delete.clicked.connect(self.on_delete)
        self.btn_restore.clicked.connect(self.on_restore)
        self.btn_pause.clicked.connect(self.on_pause)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_apply_all.clicked.connect(self.model.apply_auto_to_all)
        self.btn_undo.clicked.connect(self.model.undo_last)
        self.btn_keep.clicked.connect(lambda: self._set_keep_of_selection())
        self.btn_mark.clicked.connect(lambda: self._toggle_of_selection())
        self.btn_delete_one.clicked.connect(lambda: self._delete_of_selection())
        self.sld.valueChanged.connect(
            lambda v: self.lbl_sld.setText(f"{v / 100:.2f}"))
        self.filter_list.currentItemChanged.connect(self._on_filter_changed)
        self.cmb_theme.currentIndexChanged.connect(self._on_theme_changed)
        self.sld_thumb.valueChanged.connect(self._on_thumb_size_changed)
        self.thumbs.loaded.connect(lambda _t: self.view.viewport().update())
        self.preview_cache.loaded.connect(lambda _t: self.preview.update())
        self.model.counts_changed.connect(self._refresh_counts)
        self.view.selectionModel().currentChanged.connect(self._on_current_changed)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_view_width()

    def closeEvent(self, event) -> None:
        self.thumbs.shutdown()
        self.preview_cache.shutdown()
        self.preview.shutdown_anim()
        super().closeEvent(event)

    def _guard_deep_mode(self, idx: int) -> None:
        if mode_at(idx) != "deep":
            self._last_mode_idx = idx
            return

        from imgdedup_core import embed as EMB

        if EMB.is_ready():
            self._last_mode_idx = idx
            return

        from imgdedup_ui._entry_symbols import ensure_model_ready

        if ensure_model_ready(self):
            self._last_mode_idx = idx
            return

        self.cmb_mode.setCurrentIndex(self._last_mode_idx)

    def _sync_mode_hint(self, _idx: int = -1) -> None:
        info = mode_info(mode_at(self.cmb_mode.currentIndex()))
        self.lbl_mode.setText(
            f"✓ {info['detects']}    ✗ 漏掉：{info['misses']}")

    def _mode_tip_text(self) -> str:
        info = mode_info(mode_at(self.cmb_mode.currentIndex()))
        return (f"【{info['name']}】{info['tag']}\n\n"
                f"做法：{info['brief']}\n"
                f"能抓到：{info['detects']}\n"
                f"会漏掉：{info['misses']}\n"
                f"开销：{info['cost']}")

    def on_pick(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择图片目录", self.root or "")
        if not d:
            return
        self.root = d
        self.ed_root.setText(d)
        self.btn_start.setEnabled(True)

    def on_start(self) -> None:
        if not self.root or (self.worker and self.worker.isRunning()):
            return
        mode = mode_at(self.cmb_mode.currentIndex())
        self._clear_selected()
        self.delegate.clear_selected()
        self._drag_anchor = None
        self.model.set_rows([])
        self.thumbs.clear()
        self.preview.show_path(None)
        self.progress.set_stages([])
        self.btn_start.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("暂停")
        self.btn_export.setEnabled(False)
        self.lbl_status.setText("扫描中…")

        self.worker = ScanWorker(self.root, mode=mode,
                                 threshold=self.sld.value() / 100.0,
                                 cache_dir=str(app_data_dir() / "cache"),
                                 parent=self)
        self.worker.stage.connect(self._on_stage)
        self.worker.log.connect(lambda s: self.lbl_status.setText(s[:120]))
        self.worker.failed.connect(self._on_failed)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.start()

    def _on_thumb_size_changed(self, value: int) -> None:
        self.lbl_thumb.setText(str(value))
        if self.delegate is None:
            return
        if self.delegate.set_thumb_size(value):
            self.view.doItemsLayout()
            self.view.viewport().update()

    def _sync_gradient(self) -> None:
        t = theme.theme(self.theme_key)
        self.page.set_stops(t["bg_top"], t["bg_mid"], t["bg_end"])
        self.page.update()

        panel = getattr(self, "sidebar_panel", None)
        if panel is not None:
            panel.set_stops(t["bg_top"], t["bg_mid"], t["bg_end"])
            panel.set_line(t["line"])
            panel.set_shadow(t["shadow"])
            panel.update()

        brand = getattr(self, "brand_text", None)
        if brand is not None:
            brand.set_colors(t["bg_top"], t["accent"], theme.BRAND_EMBER)
            brand.update()

    def _on_theme_changed(self, _idx: int = -1) -> None:
        key = self.cmb_theme.currentData() or self.theme_key
        self.theme_key = key
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, key)
        save_theme_key(key)
        self._sync_gradient()
        if self.delegate is not None:
            self.view.doItemsLayout()
        self.view.viewport().update()
        self.progress.update()

    def _on_stage(self, name: str, done: int, total: int) -> None:
        self.progress.update_stage(name, done, total)
        if total > 0:
            self.lbl_status.setText(f"{name} {done:,}/{total:,}")

    def _on_failed(self, msg: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("暂停")
        self.lbl_status.setText("扫描失败")
        QMessageBox.critical(self, "扫描失败", msg)

    def _on_finished(self, rows, meta) -> None:
        self.meta = meta or {}
        self.model.set_rows(list(rows))
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("暂停")
        self.btn_export.setEnabled(bool(rows))
        self.btn_delete.setEnabled(bool(self.model.planned()))
        n = len(rows)
        self.lbl_status.setText(
            f"完成：{self.meta.get('files', 0):,} 个文件，{n:,} 个分组"
            if n else f"完成：{self.meta.get('files', 0):,} 个文件，未发现重复")
        self._refresh_counts()
        self._sync_view_width()

    def _sync_view_width(self) -> None:
        if self.delegate is None:
            return
        if self.delegate.set_view_width(self.view.viewport().width()):
            self.view.doItemsLayout()

    def _filter_tip_text(self) -> str:
        base = "选择一个分组类型（单选）。"
        if not self.meta:
            return base
        skipped = [KIND_LABEL[k] for k in KIND_LABEL
                   if k not in mode_info(self.meta.get("mode", "")).get("kinds", ())]
        if not skipped:
            return base + "灰色的是这一档算了、但一个都没有。"
        return base + "标「未算」的是这一档不计算的：" + "、".join(skipped)

    def _rebuild_filters(self) -> None:
        prev = self._current_filter_key()

        self.filter_list.blockSignals(True)
        self.filter_list.clear()
        counts: dict[str, int] = {}
        for r in self.model.rows:
            counts[r.kind] = counts.get(r.kind, 0) + 1

        item = QListWidgetItem(f"全部 · {len(self.model.rows):,}")
        item.setData(Qt.UserRole, ALL_KINDS)
        self.filter_list.addItem(item)

        possible = set(mode_info(self.meta.get("mode", "")).get("kinds", ()))
        seen: set[str] = {ALL_KINDS}
        for kind, label in KIND_LABEL.items():
            n = counts.get(kind, 0)
            if n:
                text = f"{label} · {n:,}"
            elif kind in possible:
                text = f"{label} · 0"
            else:
                text = f"{label} · 未算"
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, kind)
            if not n:
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            self.filter_list.addItem(it)
            if n:
                seen.add(kind)

        target = prev if prev in seen else ALL_KINDS
        self._select_only(target, notify=False)
        self.filter_list.blockSignals(False)

    def _current_filter_key(self) -> str:
        it = self.filter_list.currentItem()
        if it is None:
            return ALL_KINDS
        return it.data(Qt.UserRole) or ALL_KINDS

    def _select_only(self, key: str, *, notify: bool = True) -> None:
        self._syncing = True
        self.filter_list.blockSignals(True)
        found = None
        for i in range(self.filter_list.count()):
            it = self.filter_list.item(i)
            if it.data(Qt.UserRole) == key:
                found = it
                break
        try:
            if found is not None:
                self.filter_list.setCurrentItem(found)
        finally:
            self.filter_list.blockSignals(False)
            self._syncing = False
        if notify and found is not None:
            self._on_filter_changed(found)

    def _active_kinds(self) -> Optional[set[str]]:
        if self.filter_list.count() == 0:
            return None
        key = self._current_filter_key()
        if key == ALL_KINDS:
            return None
        return {key}

    def _on_filter_changed(self, item=None) -> None:
        if self._syncing:
            return
        self.model.refilter(self._active_kinds())
        self.view.doItemsLayout()
        self._refresh_filter_state()

    def _refresh_filter_state(self) -> None:
        if self.filter_list.count() == 0:
            self.lbl_filter_state.setText("")
            return
        shown = self.model.visible_count()
        total = len(self.model.rows)
        if self._active_kinds() is None:
            self.lbl_filter_state.setText(f"显示全部 {shown:,} 组")
        elif shown == 0:
            self.lbl_filter_state.setText("当前类型无分组")
        else:
            self.lbl_filter_state.setText(f"显示 {shown:,} / {total:,} 组")


    def _refresh_counts(self) -> None:
        planned = self.model.planned()
        freed = sum(size for _p, size in planned)
        self.lbl_counts.setText(
            f"待删 {len(planned):,} 个 · 可释放 {freed / 1024**2:.1f} MB")
        self.btn_delete.setEnabled(bool(planned))
        self._rebuild_filters()

    def _current(self):
        idx = self.view.currentIndex()
        if not idx.isValid():
            return None, -1
        return self.model.row_at(idx.row()), idx.row()

    def _current_pair(self):
        row, vrow = self._current()
        anchor = self.delegate.anchor
        if row is None or anchor is None or anchor[0] != vrow:
            return None, -1
        pos = anchor[1]
        if not (0 <= pos < row.count):
            return None, -1
        return row, pos

    def _selection_in_row(self) -> tuple[Optional[int], set[int]]:
        row, vrow = self._current()
        if row is None or vrow < 0 or self.delegate is None:
            return None, set()
        poses = {p for (v, p) in self.delegate.sel_set if v == vrow}
        return vrow, poses

    def _set_selected(self, vrow: int, pos: int) -> None:
        self.delegate.set_selected(vrow, pos)
        self._after_selection_change(vrow)

    def _extend_selection(self, vrow: int, pos: int) -> None:
        anchor = self.delegate.anchor
        if anchor is None or anchor[0] != vrow:
            self._set_selected(vrow, pos)
            return
        lo, hi = sorted((anchor[1], pos))
        self.delegate.set_selection({(vrow, p) for p in range(lo, hi + 1)},
                                    anchor=(vrow, pos))
        self._after_selection_change(vrow)

    def _after_selection_change(self, vrow: int) -> None:
        self.view.setFocus(Qt.MouseFocusReason)
        idx = self.view.model().index(vrow, 0)
        if idx.isValid():
            self.view.setCurrentIndex(idx)
        self.view.viewport().update()
        self._on_current_changed(idx, None)

    def _clear_selected(self) -> None:
        if not self.delegate.sel_set:
            return
        self.delegate.clear_selected()
        self.view.viewport().update()
        self.preview.show_path(None)
        self._on_current_changed(self.view.currentIndex(), None)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.view.viewport():
            et = event.type()
            pt = event.position().toPoint() if hasattr(event, "position") else None
            hit = self.delegate.hit_test(pt) if pt is not None else None

            if et == event.Type.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    if hit is not None:
                        if event.modifiers() & Qt.ShiftModifier:
                            self._extend_selection(*hit)
                        elif hit == self.delegate.anchor:
                            self._clear_selected()
                        else:
                            self._set_selected(*hit)
                        self._drag_anchor = hit
                    else:
                        self._drag_anchor = None
                        self._clear_selected()
                    return True
                if event.button() == Qt.RightButton and hit is not None:
                    if not self.delegate.is_selected(*hit):
                        self._set_selected(*hit)
                    return False
            elif et == event.Type.Leave:
                self.delegate.set_hover(None)
                self.view.viewport().update()
            elif et == event.Type.MouseMove:
                if (self._drag_anchor is not None
                        and event.buttons() & Qt.LeftButton
                        and hit is not None
                        and hit[0] == self._drag_anchor[0]):
                    lo, hi = sorted((self._drag_anchor[1], hit[1]))
                    self.delegate.set_selection(
                        {(hit[0], p) for p in range(lo, hi + 1)},
                        anchor=self._drag_anchor)
                    self.view.viewport().update()
                    self._on_current_changed(self.view.currentIndex(), None)
                elif self._hover_row != hit:
                    self.view.viewport().setCursor(
                        Qt.PointingHandCursor if hit else Qt.ArrowCursor)
                    self._hover_row = hit
                    self.delegate.set_hover(hit)
                    self.view.viewport().update()
            elif et == event.Type.MouseButtonRelease:
                self._drag_anchor = None
            elif et == event.Type.Wheel:
                return self._on_wheel(event)
        return super().eventFilter(obj, event)

    def _on_wheel(self, event) -> bool:
        sb = self.view.verticalScrollBar()
        if sb.maximum() <= sb.minimum():
            return True

        delta = event.angleDelta().y() or event.angleDelta().x()
        pix = event.pixelDelta().y()

        if pix:
            step = max(-WHEEL_STEP_PX, min(WHEEL_STEP_PX, pix))
        elif delta:
            notches = int(delta / WHEEL_NOTCH) or (1 if delta > 0 else -1)
            step = notches * WHEEL_STEP_PX
        else:
            return True

        sb.setValue(sb.value() - step)
        event.accept()
        return True

    def _move_selection(self, dpos: int, dwrap: int, *, extend: bool) -> bool:
        vrow, poses = self._selection_in_row()
        row, _ = self._current()
        if row is None or vrow < 0:
            row0 = self.model.row_at(0)
            if row0 is None or row0.count == 0:
                return True
            self._set_selected(0, 0)
            return True

        if not poses:
            self._set_selected(vrow, 0)
            return True

        anchor = self.delegate.anchor or (vrow, 0)
        pos = anchor[1]
        n = row.count
        new_pos = pos + dpos

        if new_pos < 0 or new_pos >= n:
            new_vrow = vrow + dwrap
            if not (0 <= new_vrow < len(self.model.visible_rows())):
                return True
            nxt = self.model.row_at(new_vrow)
            if nxt is None or nxt.count == 0:
                return True
            new_pos = 0 if dwrap > 0 else nxt.count - 1
            vrow = new_vrow
            row = nxt

        if extend:
            lo, hi = sorted((anchor[1], new_pos))
            self.delegate.set_selection({(vrow, p) for p in range(lo, hi + 1)},
                                        anchor=(vrow, anchor[1]))
            self._after_selection_change(vrow)
        else:
            self._set_selected(vrow, new_pos)
        idx = self.view.model().index(vrow, 0)
        if idx.isValid():
            self.view.scrollTo(idx)
        return True

    def keyPressEvent(self, event) -> None:
        if self._handle_selection_key(event):
            return
        super().keyPressEvent(event)

    def _handle_selection_key(self, event) -> bool:
        if (self.ed_root.hasFocus() or self.sld.hasFocus()
                or self.zoom_edit.hasFocus()):
            return False
        key = event.key()
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if key == Qt.Key_Left:
            return self._move_selection(-1, -1, extend=shift)
        if key == Qt.Key_Right:
            return self._move_selection(1, 1, extend=shift)
        if key == Qt.Key_Up:
            return self._move_selection(0, -1, extend=False)
        if key == Qt.Key_Down:
            return self._move_selection(0, 1, extend=False)
        if key == Qt.Key_Space:
            self._set_keep_of_selection()
            return True
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            self._toggle_of_selection()
            return True
        if key == Qt.Key_Escape:
            self._clear_selected()
            return True
        if key == Qt.Key_A and event.modifiers() & Qt.ControlModifier:
            vrow, _ = self._selection_in_row()
            row = self.model.row_at(vrow) if vrow >= 0 else None
            if row is None:
                return False
            self.delegate.set_selection({(vrow, p) for p in range(row.count)},
                                        anchor=(vrow, 0))
            self._after_selection_change(vrow)
            return True
        return False

    def _on_context_menu(self, where: QPoint) -> None:
        vrow = self.view.indexAt(where).row()
        if vrow < 0:
            return
        row = self.model.row_at(vrow)
        if row is None:
            return

        hit = self.delegate.hit_test(where)
        if hit is not None and hit[0] == vrow and not self.delegate.is_selected(*hit):
            self._set_selected(vrow, hit[1])
        row, pos = self._current_pair()
        if row is None:
            return
        item = row.items[pos]
        is_keep = row.is_kept(pos)
        n_sel = len(self._selection_in_row()[1])
        many = n_sel > 1
        scope = f"{n_sel} 张" if many else "这一张"

        m = QMenu(self)
        act_keep = m.addAction(f"设为保留（{scope}）" if many else "设为保留")
        act_del = m.addAction(
            (f"标记删除（{scope}）" if many else "标记删除")
            if item.decision != DELETE else
            (f"取消删除标记（{scope}）" if many else "取消删除标记"))
        act_undecided = m.addAction("设为未裁决")
        m.addSeparator()
        act_del_now = m.addAction(
            f"立即删除选中的 {n_sel} 张…" if many else "立即删除这一张…")
        act_reveal = m.addAction("在文件夹中显示")
        act_copy = m.addAction(
            f"复制 {n_sel} 个文件路径" if many else "复制文件路径")

        act_del.setEnabled(not is_keep)
        act_keep.setEnabled(not is_keep)
        act_undecided.setEnabled(item.decision != UNDECIDED and not is_keep)
        act_del_now.setEnabled(not is_keep and not row.oversized)

        m.addSeparator()
        act_group_keep = m.addAction("把这一张设为全组保留")
        act_group_del = m.addAction("删除本组其余全部")
        act_group_del.setEnabled(not row.oversized)

        chosen = m.exec(self.view.viewport().mapToGlobal(where))
        if chosen is None:
            return
        _, poses = self._selection_in_row()
        if chosen is act_keep:
            if many:
                self._set_keep_of_selection()
            else:
                self.model.set_keep(vrow, pos)
        elif chosen is act_del:
            self._toggle_of_selection()
        elif chosen is act_undecided:
            self.model.set_undecided(vrow, pos)
        elif chosen is act_del_now:
            if many:
                self._delete_of_selection()
            else:
                self._delete_paths_now([item.path], source="右键删除")
        elif chosen is act_reveal:
            self._reveal(item.path)
        elif chosen is act_copy:
            from PySide6.QtWidgets import QApplication

            if many:
                txt = "\n".join(row.items[p].path for p in sorted(poses))
            else:
                txt = item.path
            QApplication.clipboard().setText(txt)
            self.lbl_status.setText("已复制路径" + (f"（{n_sel} 个）" if many else ""))
        elif chosen is act_group_keep:
            self.model.set_keep(vrow, pos)
        elif chosen is act_group_del:
            paths = [i.path for p, i in enumerate(row.items) if p != pos]
            self._delete_paths_now(paths, source="按组删除")
        self.view.viewport().update()
        self._on_current_changed(self.view.currentIndex(), None)

    def _reveal(self, path: str) -> None:
        import subprocess

        try:
            if not path or not os.path.exists(path):
                self.lbl_status.setText("打开位置失败：文件已不存在")
                return
            target = os.path.normpath(path)
            subprocess.Popen(f'explorer /select,"{target}"', shell=False)
        except Exception as e:
            self.lbl_status.setText(f"打开位置失败：{e}")

    def _on_current_changed(self, cur, _prev) -> None:
        row, vrow = self._current()
        pair_row, pos = self._current_pair()
        if row is None:
            self.lbl_title.setText("—")
            self.lbl_detail.setText("")
            self.lbl_reason.setText("")
            self.lbl_focus.setText("")
            self.btn_keep.setEnabled(False)
            self.btn_mark.setEnabled(False)
            self.btn_delete_one.setEnabled(False)
            self.preview.show_path(None)
            return

        self.lbl_title.setText(f"{KIND_LABEL.get(row.kind, row.kind)} · {row.count} 张")
        if pair_row is not None:
            it = pair_row.items[pos]
            state = ("保留" if pair_row.is_kept(pos)
                     else ("待删" if it.decision == DELETE else "未裁决"))
            self.lbl_focus.setText(self._focus_text(pos, state, it))
            self.lbl_detail.setText(
                f"文件：{os.path.basename(it.path)}\n"
                f"尺寸 {it.dims} · {it.size / 1024**2:.2f} MB\n"
                f"格式 {it.fmt or '未知'}")
            self.preview.show_path(it.path, os.path.basename(it.path),
                                   f"{it.dims} · {it.size / 1024**2:.2f} MB"
                                   f" · {it.fmt or '未知'} · {state}")
        else:
            self.lbl_focus.setText("")
            self.lbl_detail.setText("")

        self.lbl_reason.setText(self._hint_text(row))

        _, poses = self._selection_in_row()
        actionable = {p for p in poses if not row.is_kept(p)}
        self.btn_keep.setEnabled(bool(actionable))
        self.btn_mark.setEnabled(bool(actionable) and not row.oversized)
        self.btn_delete_one.setEnabled(bool(actionable) and not row.oversized)

    def _focus_text(self, pos: int, state: str, it) -> str:
        n = len(self._selection_in_row()[1])
        if n > 1:
            return (f"▸ 已选中 {n} 张（含第 {pos + 1} 张 · {state}）\n"
                    f"{os.path.basename(it.path)}")
        return f"▸ 已选中第 {pos + 1} 张 · {state}\n{os.path.basename(it.path)}"

    @staticmethod
    def _hint_text(row) -> str:
        if row.oversized:
            return "过大聚类只能人工逐张确认，不参与批量删除。"
        if row.label.startswith("含"):
            return ("本组由多个字节重复组近似合并而来，默认只留 1 张，"
                    "其余都标成待删。组里可能混着只是看着像的图，"
                    "删之前先逐张看一眼。")
        return ("左键点缩略图 = 选中；Shift+左键 / 按住拖动 = 连选多张；"
                "方向键 = 移动，Shift+方向键 = 连选，Ctrl+A = 全选本组；"
                "空格 = 设为保留，Delete = 标记删除；右键 = 更多操作。")

    def _set_keep_of_selection(self) -> None:
        vrow, poses = self._selection_in_row()
        row = self.model.row_at(vrow) if vrow >= 0 else None
        if row is None or not poses:
            return
        if len(poses) == 1:
            pos = next(iter(poses))
            if row.is_kept(pos):
                self.model.toggle_keeps(vrow, poses)
            else:
                self.model.set_keep(vrow, pos)
        else:
            self.model.toggle_keeps(vrow, poses)
        self.view.viewport().update()
        self._on_current_changed(self.view.currentIndex(), None)

    def _toggle_of_selection(self) -> None:
        vrow, poses = self._selection_in_row()
        row = self.model.row_at(vrow) if vrow >= 0 else None
        if row is None or not poses or row.oversized:
            return
        self.model.toggle_delete_many(vrow, poses)
        self.view.viewport().update()
        self._on_current_changed(self.view.currentIndex(), None)

    def _delete_of_selection(self) -> None:
        vrow, poses = self._selection_in_row()
        row = self.model.row_at(vrow) if vrow >= 0 else None
        if row is None or not poses:
            return
        paths = [row.items[p].path for p in sorted(poses) if not row.is_kept(p)]
        if not paths:
            QMessageBox.information(self, "没有可删除项",
                                    "选中的都是「保留」项，请先取消保留再删除。")
            return
        self._delete_paths_now(paths, source="删除选中")

    def _catalog_or_none(self):
        if self._catalog is not None:
            return self._catalog
        try:
            from imgdedup_core import catalog as C

            self._catalog = C.Catalog(str(app_data_dir() / "catalog.db"))
        except Exception:
            self._catalog = None
        return self._catalog

    def _delete_paths_now(self, paths: list[str], *, source: str = "删除") -> None:
        from imgdedup_core import action as A

        paths = [p for p in paths if p]
        if not paths:
            return
        size_of = {it.path: it.size
                   for row in self.model.rows for it in row.items}
        reclaim = sum(size_of.get(p, 0) for p in paths)
        cat = self._catalog_or_none()
        remembered = A.load_decision(cat.get_setting) if cat else None

        d = ask_delete(self, len(paths), reclaim, paths[:200],
                       remembered=remembered,
                       default_target=A.default_target_dir(self.root))
        if d is None:
            return
        if d.remember and remembered is None and cat is not None:
            A.save_decision(cat.set_setting, d)

        plan = A.plan_from_paths([(p, size_of.get(p, 0)) for p in paths],
                                 root=self.root, kind="ui")
        if not plan.items:
            QMessageBox.information(self, "没有可删除项",
                                    "\n".join(plan.blocked) or "清单为空")
            return
        try:
            res = A.execute(plan, d, manifest_dir=str(app_data_dir() / "manifests"))
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"{type(e).__name__}: {e}")
            return

        self.last_manifest = res.manifest
        self.btn_restore.setEnabled(True)
        self.lbl_status.setText(f"{source}：已删除 {res.ok:,} 个"
                                f"（{res.bytes / 1024**2:.1f} MB）"
                                f"{'，失败 ' + str(res.failed) if res.failed else ''}")
        gone = set(paths)
        for row in self.model.rows:
            kept = {row.items[p].path for p in row.keeps if p < row.count}
            row.items = [i for i in row.items if i.path not in gone]
            row.keeps = {p for p, i in enumerate(row.items) if i.path in kept}
            if not row.keeps and row.items:
                row.keeps = {0}
            row.auto_decide()
        kept_rows = [r for r in self.model.rows if r.count >= 2]
        dropped = len(self.model.rows) - len(kept_rows)
        self._clear_selected()
        self.model.set_rows(kept_rows)
        self._refresh_counts()
        QMessageBox.information(
            self, "完成",
            f"{res.describe()}\n"
            f"{'清理了 ' + str(dropped) + ' 个已不足 2 张的分组\n' if dropped else ''}"
            f"\n已写入撤销记录：\n{os.path.basename(res.manifest)}\n"
            f"随时可用「撤销上次删除」还原。")

    def on_delete(self) -> None:
        planned = self.model.planned()
        if not planned:
            return
        self._delete_paths_now([p for p, _s in planned], source="批量删除")

    def on_pause(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            return
        paused = self.worker.toggle_pause()
        self.btn_pause.setText("继续" if paused else "暂停")
        self.lbl_status.setText("已暂停（进度已保留）" if paused else "继续扫描…")

    def on_export(self) -> None:
        if not self.model.rows:
            return
        from . import report as R

        base = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(base):
            base = str(app_data_dir())
        default = os.path.join(base, "ImgDedup-报告.html")
        path, _chosen = QFileDialog.getSaveFileName(
            self, "导出报告", default, "HTML 报告 (*.html);;CSV 表格 (*.csv)")
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                n = R.export_csv(self.model.rows, path)
            else:
                n = R.export_html(self.model.rows, self.meta, path)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"{type(e).__name__}: {e}")
            return
        self.lbl_status.setText(f"已导出 {n:,} 组到 {path}")

    def on_restore(self) -> None:
        from imgdedup_core import action as A

        if not self.last_manifest:
            return
        r = A.undo(self.last_manifest)
        self.lbl_status.setText(f"已还原 {r.restored:,} 个"
                                f"{'，跳过 ' + str(r.skipped) if r.skipped else ''}"
                                f"{'，失败 ' + str(r.failed) if r.failed else ''}")
        if r.notes:
            QMessageBox.information(self, "撤销结果", "\n".join(r.notes[:12]))
        self.btn_restore.setEnabled(False)
        self.last_manifest = ""


