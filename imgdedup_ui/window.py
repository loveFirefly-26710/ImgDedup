"""主窗口：三栏布局 + 网格虚拟化 + 删除裁决 + 多段进度。

布局：
    工具栏（扫描源 / 模式 / 阈值 / 开始 / 导出）
    ├─ 左：分组筛选 + 保护规则
    ├─ 中：结果网格（QListView + 自绘 delegate，Qt 原生虚拟化）
    └─ 右：选中项详情 + 保留 / 标记删除
    底部：多段进度 + 状态 + 暂停/取消

启动路径上不导入核心引擎：cv2/numpy 的导入是秒级的，会毁掉首屏。
所有 core 导入都在后台线程或用户动作之后。
"""

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
# 只导入真正用到的：侧栏品牌区移除后 `BrandDot`/`BrandText` 不再出现在
# 本文件里。两个类本身仍留在 widgets.py，因为 `tests/test_theme.py` 会直接
# 实例化它们做断言，`dev/check_frozen.py` 也把它们列为应存在于包里的符号。
from .widgets import (GradientPage, GroupDelegate, InstantToolTip,
                      PanelSurface, PreviewPane, StageProgress)

#: 预览大图的解码边长。与 `PreviewPane.STILL_SIDE` 必须一致：
#: 滚轮最大放大到 16x，如果预览只解 800px，放大后立刻糊成马赛克。
#: 1024px 在 260px 宽的侧栏里够放大到约 4x 仍清晰，再往上就是浏览细节极限了。
PREVIEW_SIZE = PreviewPane.STILL_SIDE
from .worker import MODES, MODE_ORDER, ScanWorker, mode_at, mode_info

KIND_LABEL = {
    "exact": "精确重复",
    "perceptual": "近似重复",
    "semantic": "语义相似",
    "cluster": "聚类（过合并）",
}

#: 「全部」筛选项的哨兵值。
#:
#: 不能用 `None`：`None` 已经被 没有分类被勾选 用掉了，两者含义完全相反
#: （不过滤 vs 全过滤）。曾经把「全部」的 UserRole 设成 None，于是只勾「全部」
#: 时收集到的 kinds 是空集 → `refilter(set())` → 0 行，分组整片消失。
ALL_KINDS = "__all__"

#: 缩略图边长的可调范围。下限 64 是为了让"概览模式"一屏能塞下几千张；
#: 上限 256 是经验值：再大一屏放不下几张，滚动手感陡降。
THUMB_MIN, THUMB_MAX, THUMB_DEFAULT = 64, 256, 112

#: 相似度滑块的默认值（百分比整数）。
#:
#: 必须与 `imgdedup_core.embed.DEFAULT_THRESHOLD`（0.97）一致。
#: 由 `tests/test_packaging.py::TestThresholdConsistency` 钉住。
#: 别按"滑块的中间值"来给：这个图库自相似度极高，实测 0.92 会合并出
#: 1554 张的巨型组（半个图库），阈值扫描的拐点在 0.97。
SIM_DEFAULT_PCT = 97

#: 滚轮每"一格"滚动的像素数。
#:
#: 必须显式接管，不能让 Qt 按默认算法走。`ScrollPerPixel` 模式下 Qt 把
#: 滚动条 `singleStep` 设成"一个 item 的高度"（一行卡片，缩略图 112px 时约
#: 1018px），滚轮再乘上 `wheelScrollLines`（系统默认 3）= 每次 3000px 以上。
#: 而一屏通常只有几百像素高 → 一次滚轮直接跳到列表末尾，中间的分组
#: 全被略过。这正是用户报的"一次滚动连续跳过多个页面"。
#:
#: 取 52px 的依据：略小于最小卡片的行高（64px 缩略图 → 一行约 140px），
#: 保证"一格滚轮"的位移肉眼可辨、又不至于让人怀疑滚轮坏了；
#: 一格 ≈ 1/3 张卡片，连滚三格刚好推进约一张卡片。
WHEEL_STEP_PX = 52

#: 一格滚轮（`angleDelta().y()` 通常是 ±120）对应多少步 `WHEEL_STEP_PX`。
#: 保持 1 是"所见即所得"：用户滚一格，画面就动一格。
WHEEL_NOTCH = 120


def app_data_dir() -> Path:
    """应用数据目录。刻意不放项目里：那是运行时数据，不是项目文件。"""
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
        #: delegate 在 `_build_center()` 里建：它需要视图引用做坐标换算，
        #: 而视图是 `_build_center()` 创建的。见 `_build_ui()`。
        self.delegate: Optional[GroupDelegate] = None

        self.worker: Optional[ScanWorker] = None
        self.root: str = ""
        self.meta: dict = {}
        self.last_manifest: str = ""
        self._catalog = None
        #: 上一行缩略图所在的可见行：「再点一次取消选中」要用它判断
        self._last_sel_vrow: int = -1
        self._hover_row = None
        #: 按住左键拖拽连选的起点 `(可见行, 行内下标)`；None = 没在拖
        self._drag_anchor: Optional[tuple[int, int]] = None
        #: 正在同步筛选项的选中状态：用来短路 `itemChanged` 回调
        self._syncing: bool = False
        #: 当前主题 key
        self.theme_key: str = theme.load_theme_key()

        self._build_ui()
        self._wire()
        #: 启动时把渐变背景对齐到已保存的主题。
        #: 必须在这里补一次：`apply_theme()` 是在 `MainWindow()` 之前
        #: 调的（否则品牌圆点那些构造期就读色的控件会闪一帧旧色），
        #: 那时 `GradientPage` 还不存在，`topLevelWidgets()` 里找不到它。
        #: 结果就是"按钮是琥珀的、背景还是薄荷的"这种半截状态。
        self._sync_gradient()
        #: 下拉框的弹出列表行色由自绘 delegate 负责，而 `apply_theme()` 是在
        #: `MainWindow()` 之前 调的：那时 `cmb_mode` 还没出生，
        #: `_apply_combo_style()` 扫不到它。所以这里必须补一次，
        #: 否则弹出列表的选中行会退回 Qt 默认（又是一条看不出内容的浅色行）。
        from .theme import _apply_combo_style
        _apply_combo_style(QApplication.instance(), theme.theme(self.theme_key))

    # ============================================================ 构建
    def _build_ui(self) -> None:
        #: 最底层是渐变页：流萤的"冷→暖"背景（QSS 给不了渐变，只能自绘）。
        #: 工具栏/三栏/底栏全部叠在它上面，各自背景设成 transparent 透出来。
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
        #: 右栏现在装的是预览大图（原来是纯信息面板），给宽一点才看得清
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

        #: 黄框位置：模式差异说明。不是工具提示：工具提示要悬停才看得见，
        #: 而"三档到底差在哪"恰恰是用户选档时最需要当场看到的信息。
        #: 做成常驻小标签，随下拉框切换实时更新，宽度自适应不挤跑滑块。
        self.lbl_mode = QLabel()
        self.lbl_mode.setObjectName("modeHint")
        self.lbl_mode.setWordWrap(False)
        self.lbl_mode.setTextInteractionFlags(Qt.NoTextInteraction)
        lay.addWidget(self.lbl_mode, 1)
        #: 上一次用户选中的档位 index：「深度」拿不到模型时拨回这里。
        self._last_mode_idx = 0
        # 拦截必须挂 `activated`（只在用户操作时触发），
        # 不能挂 `currentIndexChanged`（程序化 `setCurrentIndex` 也会触发）。
        #
        # 挂错的后果实测过：测试 / 开发脚本把档位设成「深度」时会弹出
        # 模态对话框 `exec()`，缺模型就永远阻塞：CI 会挂到 job 超时。
        # （实测：`setCurrentIndex` 只发 `currentIndexChanged`，不发 `activated`。）
        self.cmb_mode.activated.connect(self._guard_deep_mode)
        # 提示文字要跟任何变化走，包括我们自己的"拨回去"。
        self.cmb_mode.currentIndexChanged.connect(self._sync_mode_hint)
        self._sync_mode_hint()

        #: 即时提示气泡（需求：悬停/聚焦当场弹出，且常驻不自动消失）。
        #: 必须由 self 持有强引用：只挂到控件上是挂不住的，
        #: `attach()` 只把 self 记进 widget 的列表，Python 侧没人引用就会被 GC，
        #: 表现成"悬停没反应"（对象已销毁，事件过滤器还活着但控件没了）。
        #: provider 传 callable 而不是快照字符串：切档后气泡里必须是新档位的说明。
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

        #: 主题切换。桌面工具不做 hue 滑块（那是展示型网站的玩法），
        #: 给 5 套预设足够了：见 docs/流萤主题美化设计方案.md
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
        #: 外面套一层 `PanelSurface`：一块有边界感的面板。
        #: 竖直渐变 + 圆角 + 投影，让侧栏"浮"在渐变背景之上。
        #: 里面那层 `box` 保持透明，内容照常排。
        #
        # 这里曾经有一行品牌区（`● ImgDedup` 圆点 + 渐变字）。
        # 已按需求移除：窗口标题栏本身就写着「ImgDedup」，侧栏再重复一遍
        # 是纯噪音，还白占一行高度。品牌露出由标题栏与应用图标承担。
        # 移除后不补占位：`分组筛选` 直接顶到面板上沿，视觉上更干净。
        # （`BrandDot`/`BrandText` 两个类保留：见文件头导入处的说明。）
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
        #: 单选：点一项就换一项，不需要按住 Ctrl
        self.filter_list.setSelectionMode(QAbstractItemView.SingleSelection)
        lay.addWidget(self.filter_list, 1)

        #: 和模式说明一样走即时气泡：全界面提示的行为必须一致。
        #: 一部分是"悬停即弹、常驻"，一部分还是"等一秒、十秒后自己没了"，
        #: 用户只会觉得"这软件的提示时灵时不灵"。
        self.tip_filter = InstantToolTip(self)
        self.tip_filter.attach(self.filter_list, "选择一个分组类型（单选）")

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

        # ---- 缩略图大小 ----
        # 放在左栏底部：左边本来就是"设置区"，放这儿比塞进工具栏自然，
        # 也不会和工具栏那几个扫描参数抢注意力。
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
        """中间栏：结果网格。

        预览大图与信息面板都在右侧栏（见 `_build_right()`），
        这里只剩网格，不再套竖向 `QSplitter`。
        """
        self.view = QListView()
        self.view.setModel(make_list_model(self.model))
        # delegate 必须在视图存在之后创建：它要持有视图引用做命中测试
        self.delegate = GroupDelegate(self.model, self.thumbs, self.view)
        self.view.setItemDelegate(self.delegate)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setUniformItemSizes(False)
        self.view.setSpacing(0)
        #: 滚轮由我们自己的事件过滤器接管（见 `_on_wheel`）。
        #: 不能交给 Qt 默认行为：`ScrollPerPixel` 下 Qt 把单步设成"一行卡片的高度"
        #: （缩略图 112px 时高达 1018px），再乘 `wheelScrollLines`(3) 就是三千多像素。
        #: 而整个可滚动高度常常只有几百像素，一次滚轮直接见底，
        #: 中间的分组全被跳过去了。用户报的"一次滚动跳过多个页面"即此。
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.verticalScrollBar().setSingleStep(WHEEL_STEP_PX)
        # 左键点缩略图 = 切换「待删 / 未裁决」，空白处 = 取消当前选中缩略图。
        # 光靠 delegate 画不出来「点到了哪一张」，必须让视图知道缩略图的命中测试。
        self.view.viewport().installEventFilter(self)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().setCursor(Qt.PointingHandCursor)
        # 右键菜单：删除 / 设为保留 / 打开文件位置
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        return self.view

    def _build_right(self) -> QWidget:
        """右侧栏：上=预览大图，下=选中项信息 + 操作按钮。

        用 `QSplitter(Vertical)` 而不是写死高度：核对两张 4K 图的细节时
        要把预览拉大，快速过一遍时要收窄留给信息。写死必然两头不讨好。
        """
        #: 预览走独立缓存（大图、容量小），不与网格共享：见 PreviewPane 说明
        self.preview_cache = ThumbCache(size=PREVIEW_SIZE, capacity=12,
                                        parent=self)
        self.preview = PreviewPane(self.preview_cache)

        #: 预览区 = 预览框 + 下方一行缩放控件，包成一个容器再进 splitter。
        #: 不能把这两个都 `addWidget` 进 splitter：那样 splitter 会多出
        #: 一格，拖动分隔条时"缩放控件"会被单独拉扯、甚至拖成 0 高看不见。
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

        #: 当前选中的是第几张：没有这一行，用户无从判断"点中了没有"
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

        #: 后两个是"批量裁决"类操作，用得比上面三个少，并排放省高度。
        #: 侧栏窄，5 个通栏按钮会把信息区顶出滚动条。
        pair = QHBoxLayout()
        pair.setSpacing(6)
        self.btn_apply_all = QPushButton("全部按推荐裁决")
        self.btn_undo = QPushButton("撤销所有裁决")
        pair.addWidget(self.btn_apply_all)
        pair.addWidget(self.btn_undo)
        lay.addLayout(pair)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(pbox)

        #: 信息区内容随选中项变化（多选时 lbl_focus 会折成两行），但有上限，
        #: 不会无限增长，所以不用滚动区：滚动条在窄侧栏里既占地方又难看，
        #: 而且 `WordWrap` 标签的最小高度 Qt 算不准，会无谓地常驻滚动条。
        #: 直接放进 splitter，靠 `setMinimumHeight` + `setCollapsible(False)`
        #: 保证按钮永远露得出来。
        vsplit.addWidget(info)

        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        info.setMinimumHeight(300)
        vsplit.setSizes([434, 300])
        # `setChildrenCollapsible(False)` 对 QSplitter 不生效，
        # 必须对每个子控件再 `setCollapsible(i, False)` 一次。
        # 预览框被拖没了用户找不到它，也不知道怎么恢复。
        vsplit.setChildrenCollapsible(False)
        for i in range(vsplit.count()):
            vsplit.setCollapsible(i, False)
        self.right_split = vsplit

        #: 外层容器只负责边距：splitter 自己不带 margin
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 10, 12, 10)
        outer.setSpacing(0)
        outer.addWidget(vsplit)
        return box

    def _build_zoom_row(self) -> QHBoxLayout:
        """预览框下方的缩放控件：百分比输入 + 重置。

        100% 的语义是适应窗口，不是"原始像素 1:1"（见 `PreviewPane`
        的说明）。提示文案必须照这个说，否则用户按"100% 应该等于原图
        大小"去理解，会以为功能坏了。
        """
        row = QHBoxLayout()
        row.setSpacing(6)

        lbl = QLabel("缩放")
        lbl.setStyleSheet("color:#888780;")
        row.addWidget(lbl)

        self.zoom_edit = QLineEdit("100%")
        #: 定宽 + 居中。宽度按最长的可能值定：`ZOOM_MAX = 16.0` → "1600%"
        #: （5 字符）。68px 减去 QSS 的左右 padding 还剩约 52px，正常字体下
        #: "1600%" 约 38px，有富余。
        #: 改 `PreviewPane.ZOOM_MAX` 到 100x 以上会变成 6 字符，
        #: 这里就要跟着调：`test_input_wide_enough_for_max_zoom` 守着这条。
        self.zoom_edit.setFixedWidth(68)
        self.zoom_edit.setAlignment(Qt.AlignCenter)
        row.addWidget(self.zoom_edit)

        self.btn_zoom_reset = QPushButton("重置")
        self.btn_zoom_reset.setFixedWidth(52)
        row.addWidget(self.btn_zoom_reset)

        #: 右侧留白吸掉多余宽度，免得两个控件被拉开
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
        """把预览的实际倍率写回输入框。

        判据是 `isModified()`，不是 `hasFocus()`。

        两者差别很关键：`hasFocus()` 只说明"光标在这个框里"，
        而用户完全可能点了一下框（拿到焦点）却没打字，然后把鼠标移到
        预览图上滚轮缩放：这时用 `hasFocus()` 判断就会一直跳过回写，
        框里停着旧数值，看起来像"输入框坏了"。

        `isModified()` 只表示"用户改过内容"：Qt 的 `setText()` 会把它
        清回 False，所以程序回写之后下一轮同步照常进行；而用户打了字就是
        True，同步被挡住：这正是我们要保护的场景（正想输 150、刚打完
        "15"，手滑滚一下滚轮，不该把前两个字符冲掉）。

        `force=True` 是"刚提交/刚重置"那条路径，此时必须回写：
        用来把 "1.5" / "150" 归一成 "150%"，并把越界输入显示成夹取后的值。
        """
        if not force and self.zoom_edit.isModified():
            return
        self.zoom_edit.setText(f"{zoom:.0%}")

    def _apply_zoom_from_edit(self) -> None:
        """解析百分比输入框并应用；非法输入回退到当前倍率。"""
        raw = self.zoom_edit.text().strip()
        for suffix in ("%", "％"):
            raw = raw.rstrip(suffix)
        try:
            pct = float(raw.strip())
        except ValueError:
            pct = float("nan")
        # `not (pct > 0)` 同时挡住 nan / 0 / 负数（nan 的比较恒为 False）
        if not (pct > 0):
            self._sync_zoom_edit(self.preview.zoom, force=True)
            return
        self.preview.set_zoom(pct / 100.0)
        # 回写夹取后的真实值：输入 "9999" 会显示成 "1600%"（上限）
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

    # ============================================================ 事件
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
        #: 单选：换了一项就重筛。`currentItemChanged` 在 `clear()` 时也会发，
        #: 但那时 `_syncing`/`blockSignals` 已挡住，不会误触发。
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
        """关窗前把后台线程收干净。

        见 `ThumbCache.shutdown()`：不等它收尾，任务回来 emit 时信号已被
        Qt 销毁，会抛「Signal source has been deleted」。

        预览框的动图解码线程也必须等（`shutdown_anim`）。
        不等的话不是"打印点噪音"这么轻：`QThread` 在 `isRunning()` 时
        被销毁会让 Qt 直接 `abort()`，没有回溯、退出码 127，
        用户看到的就是"切几个 GIF 之后软件闪退"。
        """
        self.thumbs.shutdown()
        # 预览缓存是独立线程池，也必须单独收：漏掉它照样会刷
        # "Signal source has been deleted"。
        self.preview_cache.shutdown()
        # 动图解码走的是独立 QThread（不是缓存那个线程池），漏掉它就会崩。
        self.preview.shutdown_anim()
        super().closeEvent(event)

    # ============================================================ 扫描
    def _guard_deep_mode(self, idx: int) -> None:
        """用户手动选了「深度」时确认 CLIP 模型在位；缺了当场问用户。

        只挂 `activated`（只有用户操作才触发），不能挂
        `currentIndexChanged`：后者程序化 `setCurrentIndex` 也会触发，
        于是"测试 / 脚本把档位设成深度"会弹出模态对话框卡死。
        实测过：全新克隆里跑测试，整个套件就挂在这一条上（CI 会挂到超时）。

        为什么不像 cv2 那样在启动时直接挡：模型只服务「深度」这一档，
        快速 / 精确两档完全不需要它。为了一个可选档位拦住整个程序启动，
        代价太大：用户可能压根不用深度模式。

        用户选「暂不下载」时，把下拉框拨回上一次用户选中的那一档 ——
        停在"深度"却不下载，点开始扫描会静默跳过语义层，
        用户会以为深度模式生效了。
        """
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

        # 没拿到模型 → 拨回上一次用户选中的档位。
        # 这里不需要防重入：`setCurrentIndex` 只发 `currentIndexChanged`、
        # 不会再触发 `activated`（实测过），所以不会递归回来。
        self.cmb_mode.setCurrentIndex(self._last_mode_idx)

    def _sync_mode_hint(self, _idx: int = -1) -> None:
        """把当前档位的差异写到说明标签上。文案全部来自 `worker.MODES`。

        只展示「能抓到什么 / 会漏什么」两句，这是用户做选择时唯一
        真正关心的取舍。耗时那种话术（"较慢"/"最快"）放进悬停气泡，
        因为它的绝对值取决于图量，写在界面上反而误导。
        """
        info = mode_info(mode_at(self.cmb_mode.currentIndex()))
        self.lbl_mode.setText(
            f"✓ {info['detects']}    ✗ 漏掉：{info['misses']}")

    def _mode_tip_text(self) -> str:
        """模式说明气泡的文案。每次弹出时现取，所以切档立刻反映。

        不能用 `setToolTip()`：Qt 内建提示有两条写死的时序 ——
        悬停约 1 秒才弹（`toolTipWakeUp`），弹出约 10 秒后自消
        （`toolTipFallAsleep`）。需求要的是"即时弹出 + 常驻不消失"，
        这两条时序在 Qt 里改不掉，只能换成自绘浮层。
        """
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
        # 用 `mode_at()` 而不是 `index == 2`：早先那种写法让"快速"和
        # "精确"跑同一条流水线（都是全量哈希），下拉框的"精确"是假的。
        # 真实差异现在定义在 `worker.MODES`，UI 说明与行为同源。
        mode = mode_at(self.cmb_mode.currentIndex())
        self._clear_selected()
        self.delegate.clear_selected()
        self._last_sel_vrow = -1
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
        """拖动滑块改缩略图大小。

        必须 `doItemsLayout()`：卡片高度是由缩略图尺寸派生的，
        只改 delegate 的属性而不重布局，新尺寸的缩略图会画到下一张卡片头上
        （离屏截图里一眼可见，纯逻辑测试测不出来）。
        """
        self.lbl_thumb.setText(str(value))
        if self.delegate is None:
            return
        if self.delegate.set_thumb_size(value):
            self.view.doItemsLayout()
            self.view.viewport().update()

    # ============================================================ 主题
    def _sync_gradient(self) -> None:
        """把自绘容器（背景页 / 侧栏面板 / 品牌字）的配色对齐到当前主题。

        `apply_theme()` 只能通过 `app.topLevelWidgets()` 反查窗口，而启动
        那一刻窗口还没建，所以窗口自己也得补这一下。
        """
        t = theme.theme(self.theme_key)
        self.page.set_stops(t["bg_top"], t["bg_mid"], t["bg_end"])
        self.page.update()

        panel = getattr(self, "sidebar_panel", None)
        if panel is not None:
            panel.set_stops(t["bg_top"], t["bg_mid"], t["bg_end"])
            panel.set_line(t["line"])
            panel.set_shadow(t["shadow"])
            panel.update()

        # 侧栏品牌区已移除（见 `_build_left` 说明）。`brand_text` 用
        # `getattr` 兜底是因为窗口之外可能有人自己造一个 BrandText 挂上来
        # （测试会），窗口自己不再建它。
        brand = getattr(self, "brand_text", None)
        if brand is not None:
            brand.set_colors(t["bg_top"], t["accent"], theme.BRAND_EMBER)
            brand.update()

    def _on_theme_changed(self, _idx: int = -1) -> None:
        """切主题：换色 + 存偏好 + 重绘自绘控件。"""
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
        # 数据到位后补一次宽度同步。
        # `resizeEvent` 在窗口首次显示时可能早于 `set_rows()`，
        # 那时 `set_view_width()` 算出的列数是基于 0 行的：结果缩略图
        # 只排一列、右边留一大片空白（离屏截图里一眼可见）。
        self._sync_view_width()

    def _sync_view_width(self) -> None:
        """按当前视口宽度重算每行放几张缩略图，并重新布局。"""
        if self.delegate is None:
            return
        if self.delegate.set_view_width(self.view.viewport().width()):
            self.view.doItemsLayout()

    # ============================================================ 筛选与统计
    def _rebuild_filters(self) -> None:
        """重建筛选项（单选）。

        与改造前的区别：以前是「全部 ⟺ 所有分类都勾选」的联动复选框，
        现在退化成一根单选框：「全部」或任意一个分类，二选一。

        这里绝不能无脑重置成「全部」选中：那会在每次裁决后把用户
        当前选中的分类悄悄打回全部，表现为「我刚选了近似重复，
        改了一张图又跳回全部了」。保留当前选择，首次出现才默认「全部」。
        """
        prev = self._current_filter_key()

        self.filter_list.blockSignals(True)
        self.filter_list.clear()
        counts: dict[str, int] = {}
        for r in self.model.rows:
            counts[r.kind] = counts.get(r.kind, 0) + 1

        item = QListWidgetItem(f"全部 · {len(self.model.rows):,}")
        item.setData(Qt.UserRole, ALL_KINDS)
        self.filter_list.addItem(item)

        seen: set[str] = {ALL_KINDS}
        for kind, label in KIND_LABEL.items():
            if kind not in counts:
                continue
            it = QListWidgetItem(f"{label} · {counts[kind]:,}")
            it.setData(Qt.UserRole, kind)
            self.filter_list.addItem(it)
            seen.add(kind)

        # 上次选的分类这次不存在了（比如关掉语义层后 semantic 组消失）→ 回落「全部」
        target = prev if prev in seen else ALL_KINDS
        self._select_only(target, notify=False)
        self.filter_list.blockSignals(False)

    def _current_filter_key(self) -> str:
        """当前选中的筛选项 key；一项都没选中时返回 `ALL_KINDS`。"""
        it = self.filter_list.currentItem()
        if it is None:
            return ALL_KINDS
        return it.data(Qt.UserRole) or ALL_KINDS

    def _select_only(self, key: str, *, notify: bool = True) -> None:
        """把选中项切到 `key` 那一项。

        `notify=False` 时只改选中、不触发筛选（用于 `_rebuild_filters()`
        内部：那儿外面会自己调一次）。
        """
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
        """当前选中对应的过滤集合。

        返回 `None` 表示不过滤（「全部」选中，或还没建过筛选项）；
        返回空集表示全过滤（一张也不显示，理论上到不了）。
        """
        if self.filter_list.count() == 0:
            return None                      # 还没建过筛选项 → 全部显示
        key = self._current_filter_key()
        if key == ALL_KINDS:
            return None                      # 「全部」→ 不过滤
        return {key}

    def _on_filter_changed(self, item=None) -> None:
        """筛选项变了（单选：只可能是换了一个）。

        历史坑：改造前「全部」的 UserRole 是 None，收集勾选时被 `if k and` 跳过，
        于是只勾「全部」→ kinds 为空集 → `refilter(set())` → 0 行，分组整片消失。
        现在「全部」走 `ALL_KINDS` 哨兵并在 `_active_kinds()` 里直接返回 None，
        与「没有分类被勾选」彻底分开。
        """
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
        s = self.model.stats()
        self.lbl_counts.setText(
            f"待删 {s['to_delete']:,} 个 · 可释放 {s['reclaim'] / 1024**2:.1f} MB")
        self.btn_delete.setEnabled(s["to_delete"] > 0)
        self._rebuild_filters()

    # ============================================================ 选中
    def _current(self):
        idx = self.view.currentIndex()
        if not idx.isValid():
            return None, -1
        return self.model.row_at(idx.row()), idx.row()

    def _current_pair(self):
        """当前主选中的 `(分组行, 行内下标)`；没有选中缩略图时 `(None, -1)`。

        分组级选中和缩略图级选中是两件事。以前只有前者，所以
        「设为保留 / 标记删除」只能写死操作 `row.keep + 1` ——
        用户根本没法指定"就删这一张"。

        多选时这里返回主选中项（最后一次点中的那张），右侧详情面板用它。
        批量操作请走 `_selection_in_row()`。
        """
        row, vrow = self._current()
        anchor = self.delegate.anchor
        if row is None or anchor is None or anchor[0] != vrow:
            return None, -1
        pos = anchor[1]
        if not (0 <= pos < row.count):
            return None, -1
        return row, pos

    def _selection_in_row(self) -> tuple[Optional[int], set[int]]:
        """当前选中集合中、属于可见行的那些：`(可见行下标, {行内下标})`。

        选中集合理论上可能跨行（Shift 拖过卡片边界），但所有批量操作
        （设为保留 / 标记删除）都只对一个分组有意义：跨组一起操作
        没有语义。所以这里只取当前行的那部分，跨行的选中仍然保留在
        集合里、仍然高亮，只是不参与本次操作。

        `_current()` 返回的是 `(row, vrow)`：行在前、下标在后。
        反着解包会拿到 `GroupRow` 当行号用，典型的症状是
        `'<' not supported between instances of 'GroupRow' and 'int'`。
        """
        row, vrow = self._current()
        if row is None or vrow < 0 or self.delegate is None:
            return None, set()
        poses = {p for (v, p) in self.delegate.sel_set if v == vrow}
        return vrow, poses

    def _set_selected(self, vrow: int, pos: int) -> None:
        """单选：清掉其他选中，只留这一张。"""
        self.delegate.set_selected(vrow, pos)
        self._after_selection_change(vrow)

    def _extend_selection(self, vrow: int, pos: int) -> None:
        """Shift + 左键：把「主选中项」到这一张之间同一行内的全部选中。

        跨行不支持：一行里的缩略图是网格排列，跨行连选在视觉上
        读不出「从哪到哪」，用户会误以为选中的是一段矩形区域。
        """
        anchor = self.delegate.anchor
        if anchor is None or anchor[0] != vrow:
            # 原来没有主选中项 / 主选中项在别行 → 退化成单选，别让用户
            # 的 Shift 点了个寂寞。
            self._set_selected(vrow, pos)
            return
        lo, hi = sorted((anchor[1], pos))
        # 主选中项要更新成这次点的那张：用户接着再按 Shift 时，
        # 期望是"从这次的点继续延伸"，而不是永远绕回第一次点的那张。
        self.delegate.set_selection({(vrow, p) for p in range(lo, hi + 1)},
                                    anchor=(vrow, pos))
        self._after_selection_change(vrow)

    def _after_selection_change(self, vrow: int) -> None:
        """选中变化后的统一收尾：同步视图 currentIndex + 重绘 + 刷右侧面板。"""
        # 把键盘焦点交给网格：用户刚点过缩略图，接下来的方向键
        # 应当作用于图片，而不是继续留在上面的路径输入框里。
        self.view.setFocus(Qt.MouseFocusReason)
        idx = self.view.model().index(vrow, 0)
        if idx.isValid():
            self.view.setCurrentIndex(idx)
        self._last_sel_vrow = vrow
        self.view.viewport().update()
        self._on_current_changed(idx, None)

    def _clear_selected(self) -> None:
        if not self.delegate.sel_set:
            return
        self.delegate.clear_selected()
        self.view.viewport().update()
        # 预览要显式清空：`_on_current_changed()` 只看"当前行"有没有，
        # 取消的是缩略图级选中而当前行仍在 → 它不会走清空分支，
        # 预览会停在那儿显示一张已经没被选中的图（用户以为还选着）。
        self.preview.show_path(None)
        # 必须同步刷新右侧面板：只重绘网格的话，按钮会停在
        # "选中时"的启用状态，用户点下去才发现没对象可操作。
        self._on_current_changed(self.view.currentIndex(), None)

    # ---------------------------------------------------------- 鼠标命中
    def eventFilter(self, obj, event) -> bool:
        """缩略图点击。委托是自绘的，视图并不知道每一张缩略图在哪 ——
        所以命中测试得自己做，否则「点图片高亮」根本无从谈起。

        左键点击必须 return True 把事件吃掉，否则 QListView 的默认
        行为还会照跑一遍（它会清掉 selection model 的 currentIndex，
        把刚设好的选中态冲掉）。右键则相反：一定要放行，
        让 `customContextMenuRequested` 正常发出，否则右键菜单不弹。
        """
        if obj is self.view.viewport():
            et = event.type()
            pt = event.position().toPoint() if hasattr(event, "position") else None
            hit = self.delegate.hit_test(pt) if pt is not None else None

            if et == event.Type.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    if hit is not None:
                        if event.modifiers() & Qt.ShiftModifier:
                            # Shift + 左键 = 从主选中项连选到这一张
                            self._extend_selection(*hit)
                        elif hit == self.delegate.anchor:
                            # 再点同一张 = 取消选中
                            self._clear_selected()
                        else:
                            self._set_selected(*hit)
                        #: 记下起点，供按住拖拽连选用
                        self._drag_anchor = hit
                    else:
                        # 点在卡片空白处：取消缩略图选中，但保留分组选中。
                        # 同时清掉拖拽起点：从这里开始拖不该连选。
                        self._drag_anchor = None
                        self._clear_selected()
                    return True
                if event.button() == Qt.RightButton and hit is not None:
                    # 在未选中的缩略图上右键：先把它选中，让菜单作用于它。
                    # 已经在选中集合里的则保持不动，否则多选之后右键，
                    # 会把辛苦选好的整批清成一张。
                    if not self.delegate.is_selected(*hit):
                        self._set_selected(*hit)
                    return False
            elif et == event.Type.Leave:
                self.delegate.set_hover(None)
                self.view.viewport().update()
            elif et == event.Type.MouseMove:
                # 按住左键拖过缩略图 = 连选（等同于 Shift 连选）。
                # 没有这个的话，用户想选 10 张只能点 1 下 + Shift 点 1 下，
                # 虽然够用，但拖拽是更符合直觉的路径。
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
                # 拖到卡片外再松手，也要结束拖拽态
            elif et == event.Type.MouseButtonRelease:
                self._drag_anchor = None
            elif et == event.Type.Wheel:
                return self._on_wheel(event)
        return super().eventFilter(obj, event)

    def _on_wheel(self, event) -> bool:
        """滚轮：按固定像素量平滑滚动，不交给 Qt 默认按行算。

        Qt 在 `ScrollPerPixel` 下用「一个 item 的高度」当单步，而这里的
        item 是一整行卡片（缩略图 112px 时约 1018px），再乘滚轮行数(3)
        → 一格滚轮就跳 3000px 以上，直接见底、跳过中间所有分组。

        这里改成：不管卡片多高，一格滚轮永远只走 `WHEEL_STEP_PX`。

        三个细节：
        - 用 `pixelDelta()` 优先（触控板/高精度滚轮给的是像素级增量，
          本身就是平滑的，应该原样透传，不能再乘系数）。
        - `angleDelta()` 是 1/8 度为单位，一格标准滚轮 = 120。
          按 `WHEEL_NOTCH` 归一化成"格数"，再乘 `WHEEL_STEP_PX`。
        - 必须 `accept()` 并 `return True`，否则事件继续传播、
          视图的默认实现又滚一遍 → 一动滚两倍。
        """
        sb = self.view.verticalScrollBar()
        if sb.maximum() <= sb.minimum():
            return True                     # 没得滚，吃掉即可

        # 触控板事件往往 `angleDelta` 为 0 而只有 `pixelDelta`，
        # 所以不能在 angleDelta==0 时提前 return：那会把触控板整个吃掉。
        delta = event.angleDelta().y() or event.angleDelta().x()
        pix = event.pixelDelta().y()

        if pix:
            # 高精度设备（触控板）给的是像素级增量，本身已经平滑。
            # 但不能原样透传：一次滑动可能给几百像素，等于又"跳过去"了。
            # 限幅到单格以内：保留平滑感，同时封住瞬时跳变。
            step = max(-WHEEL_STEP_PX, min(WHEEL_STEP_PX, pix))
        elif delta:
            #: 非零兜底：慢速微拨时 `int(delta/120)` 会截断成 0 导致"滚不动"，
            #: 所以任何非零增量至少走一格。
            notches = int(delta / WHEEL_NOTCH) or (1 if delta > 0 else -1)
            step = notches * WHEEL_STEP_PX
        else:
            return True                     # 两个增量都是 0，无事可做

        # Qt 的滚动条向下是 +，滚轮向前（远离用户）是 +，方向一致，直接用
        sb.setValue(sb.value() - step)
        event.accept()
        return True

    # ---------------------------------------------------------- 键盘
    def _move_selection(self, dpos: int, dwrap: int, *, extend: bool) -> bool:
        """方向键移动选中：`dpos` 行内横向步进，`dwrap` 换行时的行号增量。

        `extend=True`（按住 Shift）走连选，`False` 走单选移动。
        返回是否有选中项：没有选中时方向键应当先选中第一张，
        而不是白白吞掉按键（用户会以为键盘坏了）。
        """
        vrow, poses = self._selection_in_row()
        row, _ = self._current()
        if row is None or vrow < 0:
            row0 = self.model.row_at(0)
            if row0 is None or row0.count == 0:
                return True
            self._set_selected(0, 0)
            return True

        # 没有选中项时（刚清空、或只选了分组），第一次按方向键应当
        # 落在该组的第一张，而不是从 anchor 位置再挪一格。
        # 后者表现为「一按右键，第 1 张被跳过直接到第 2 张」。
        if not poses:
            self._set_selected(vrow, 0)
            return True

        anchor = self.delegate.anchor or (vrow, 0)
        pos = anchor[1]
        n = row.count
        new_pos = pos + dpos

        if new_pos < 0 or new_pos >= n:
            # 换行：横向越界时上下挪一行，并把光标放到该行首/尾。
            # 这里不做"空行跳过"：每张卡片都有图，不存在空行。
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
        # 保证移动后一定可见（虚拟化下目标行可能还没布局）
        idx = self.view.model().index(vrow, 0)
        if idx.isValid():
            self.view.scrollTo(idx)
        return True

    def keyPressEvent(self, event) -> None:
        """键盘操作：方向键选中、Shift+方向键连选、空格切保留、Delete 标删。

        焦点在根目录输入框里时不能吞掉方向键，否则用户没法在
        路径里移动光标。所以先看焦点在哪。
        """
        if self._handle_selection_key(event):
            return
        super().keyPressEvent(event)

    def _handle_selection_key(self, event) -> bool:
        """方向键等的处理入口。返回是否已消费该按键。

        焦点在任何文本输入框里时都不能吞按键，否则用户在框里按左右
        就是移动不了光标。实测 `QLineEdit` 自己会 `accept()` 掉方向键和空格，
        所以正常情况下事件根本传不到这里；但这条守卫仍要留着 ——
        它是这个方法自己的契约（"输入框有焦点就别动手"），
        而且 `Key_Space` 绑的是"设为保留"这个会改数据的动作，
        万一哪天输入框变成只读、或某个键 QLineEdit 不消费，就会静默误触发。

        也不能只看 hasFocus 就一律放行：窗口刚建好时焦点默认落在路径框上，
        用户点完缩略图想用键盘却毫无反应（"键盘坏了"）。所以
        点缩略图时把焦点交给视图，两边的诉求就都满足了。
        """
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
            # Ctrl+A = 选中当前这一行的全部缩略图
            vrow, _ = self._selection_in_row()
            row = self.model.row_at(vrow) if vrow >= 0 else None
            if row is None:
                return False
            self.delegate.set_selection({(vrow, p) for p in range(row.count)},
                                        anchor=(vrow, 0))
            self._after_selection_change(vrow)
            return True
        return False

    # ---------------------------------------------------------- 右键菜单
    def _on_context_menu(self, where: QPoint) -> None:
        """参数名刻意不叫 `pos`：下面 `_current_pair()` 返回的是
        `(行, 行内下标)`，早期版本写成 `row, pos = ...` 会把视口坐标
        原地覆盖成 int，于是 `mapToGlobal(pos)` 报
        `mapToGlobal(int)` 且菜单根本弹不出来。两个 `pos` 语义完全不同，
        必须用不同的名字分开。
        """
        vrow = self.view.indexAt(where).row()
        if vrow < 0:
            return
        row = self.model.row_at(vrow)
        if row is None:
            return

        hit = self.delegate.hit_test(where)
        # 已经在选中集合里的不要重设：多选之后右键，重设会把整批清成一张，
        # 用户辛苦选的十张瞬间没了。
        if hit is not None and hit[0] == vrow and not self.delegate.is_selected(*hit):
            self._set_selected(vrow, hit[1])
        row, pos = self._current_pair()
        if row is None:
            return
        item = row.items[pos]
        is_keep = row.is_kept(pos)
        n_sel = len(self._selection_in_row()[1])
        many = n_sel > 1
        #: 多选时菜单里带数量的那几条要说明作用范围
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

        # 保留项不可标删（不变量 1），失效的项直接置灰，点不到
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
        """在资源管理器里选中该文件。

        绝不能把 `explorer` 的实参写成 list。 这是本项目最隐蔽的一个坑：

            # 错误写法：会打开「文档」而不是目标目录
            subprocess.Popen(["explorer", f"/select,{p}"])

        `subprocess` 在 Windows 上会按 MSVCRT 规则给含空格的实参加引号
        （`list2cmdline`），于是命令行变成：

            explorer "/select,C:\\...\\流萤表情包 - 副本\\a.jpg"

        而 `explorer.exe` 不按这套规则解析：它把开头那个引号当成路径的一部分，
        判定整个 `/select,...` 参数非法，于是忽略它、打开默认的「文档」目录。
        路径里只要有空格就必现（中文目录 + ` - 副本` 这种最典型）。

        正确姿势是自己拼一整条命令行字符串：`explorer` 只在
        `/select,` 之后认路径，所以给路径单独加一对引号交给它即可。

        用 `shell=False` 传字符串：Windows 上 `CreateProcess` 收到字符串时
        原样传给子进程（不做任何转义），正是我们要的。
        换成 list（或 `shell=True`）都会把引号再转义一层、重新踩回上面那个坑。
        """
        import subprocess

        try:
            if not path or not os.path.exists(path):
                # 文件可能刚被删/移走：明确告诉用户，别让菜单看起来"点了没反应"。
                self.lbl_status.setText("打开位置失败：文件已不存在")
                return
            target = os.path.normpath(path)
            subprocess.Popen(f'explorer /select,"{target}"', shell=False)
        except Exception as e:  # noqa: BLE001
            self.lbl_status.setText(f"打开位置失败：{e}")

    # ---------------------------------------------------------- 右侧按钮
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
            self.lbl_focus.setText(self._focus_text(row, pos, state, it))
            self.lbl_detail.setText(
                f"文件：{os.path.basename(it.path)}\n"
                f"尺寸 {it.dims} · {it.size / 1024**2:.2f} MB\n"
                f"格式 {it.fmt or '未知'}")
            # 预览跟着主选中项走。多选时显示主选中那张：用户最后点的
            # 那张就是他要看的，而不是随随便便挑集合里第一张。
            self.preview.show_path(it.path, os.path.basename(it.path),
                                   f"{it.dims} · {it.size / 1024**2:.2f} MB"
                                   f" · {it.fmt or '未知'} · {state}")
        else:
            # 没选中任何缩略图时必须清空，不能留"推荐保留：XXX"那一段。
            # 取消选中（再点一次 / Esc / 点空白）之后当前分组行仍在，
            # 于是这里会残留一张图的文件名 / 尺寸 / 保留理由。
            # 看起来像"那一张还选着"，用户会以为取消没生效。
            # （分组标题仍保留：当前分组确实还是它，那不是"图片信息"。）
            self.lbl_focus.setText("")
            self.lbl_detail.setText("")

        self.lbl_reason.setText(self._hint_text(row))

        # 按钮可用性 = "有选中项" AND "该操作对这一批有意义"。
        # 保留项上「设为保留 / 标记删除 / 删除这一张」全都无意义，
        # 必须置灰，否则用户点下去什么也不发生，只会以为软件坏了。
        # 多选时按「集合里是否还有能操作的对象」判断，而不是看主选中项：
        # 选了 10 张其中有 1 张是保留项，按钮不该因此变灰。
        _, poses = self._selection_in_row()
        actionable = {p for p in poses if not row.is_kept(p)}
        self.btn_keep.setEnabled(bool(actionable))
        self.btn_mark.setEnabled(bool(actionable) and not row.oversized)
        self.btn_delete_one.setEnabled(bool(actionable) and not row.oversized)

    def _focus_text(self, row, pos: int, state: str, it) -> str:
        """右侧「已选中」提示。多选时要说清"选了几张"。

        多选后如果只显示主选中项那一张，用户会以为只选了一张，
        然后按「设为保留」却发现十张都变绿：必须把数量说出来。
        """
        n = len(self._selection_in_row()[1])
        if n > 1:
            return (f"▸ 已选中 {n} 张（含第 {pos + 1} 张 · {state}）\n"
                    f"{os.path.basename(it.path)}")
        return f"▸ 已选中第 {pos + 1} 张 · {state}\n{os.path.basename(it.path)}"

    @staticmethod
    def _hint_text(row) -> str:
        if row.oversized:
            return "过大聚类只能人工逐张确认，不参与批量删除。"
        return ("左键点缩略图 = 选中；Shift+左键 / 按住拖动 = 连选多张；"
                "方向键 = 移动，Shift+方向键 = 连选，Ctrl+A = 全选本组；"
                "空格 = 设为保留，Delete = 标记删除；右键 = 更多操作。")

    def _set_keep_of_selection(self) -> None:
        """把选中的一张或多张设为保留。

        多选时走 `add_keep`（追加语义）而不是 `set_keeps`（替换）：
        用户先保留 A、再框选 B、C 点「设为保留」，期望是 A/B/C 都保留，
        而不是把 A 顶掉。若选中的已全是保留项，则视为"取消保留"。
        """
        vrow, poses = self._selection_in_row()
        row = self.model.row_at(vrow) if vrow >= 0 else None
        if row is None or not poses:
            return
        if len(poses) == 1:
            pos = next(iter(poses))
            if row.is_kept(pos):
                # 单张已保留 → 取消它（但不允许把保留集合清空）
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
        """删除选中的这一张或多张：复用「执行删除」，不另开删除路径。"""
        vrow, poses = self._selection_in_row()
        row = self.model.row_at(vrow) if vrow >= 0 else None
        if row is None or not poses:
            return
        # 保留项永远不进删除清单（不变量 1）—— 多选里混进来的也剔掉，
        # 而不是整批拒绝，否则用户不知道该取消哪一张才能继续。
        paths = [row.items[p].path for p in sorted(poses) if not row.is_kept(p)]
        if not paths:
            QMessageBox.information(self, "没有可删除项",
                                    "选中的都是「保留」项，请先取消保留再删除。")
            return
        self._delete_paths_now(paths, source="删除选中")

    # ============================================================ 删除
    def _catalog_or_none(self):
        if self._catalog is not None:
            return self._catalog
        try:
            from imgdedup_core import catalog as C

            self._catalog = C.Catalog(str(app_data_dir() / "catalog.db"))
        except Exception:  # noqa: BLE001
            self._catalog = None
        return self._catalog

    def _delete_paths_now(self, paths: list[str], *, source: str = "删除") -> None:
        """按路径清单走路径 A（用户删除）。

        右键「立即删除」绝不能另开一条删除通道：那是最容易把
        「用户文件永不真删」这条底线捅破的写法。这里和「执行删除…」共用同一条：
        走 `ask_delete` 询问目标 + `plan_from_paths` 五条不变量 + `execute` 写 manifest。
        """
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
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "删除失败", f"{type(e).__name__}: {e}")
            return

        self.last_manifest = res.manifest
        self.btn_restore.setEnabled(True)
        self.lbl_status.setText(f"{source}：已删除 {res.ok:,} 个"
                                f"（{res.bytes / 1024**2:.1f} MB）"
                                f"{'，失败 ' + str(res.failed) if res.failed else ''}")
        gone = set(paths)
        for row in self.model.rows:
            # 顺序很重要：先用原始下标记住哪些路径是保留的，
            # 再过滤 items。反过来（先过滤再算）下标已经位移，会指到别人身上。
            # 表现为「删了几张之后，剩下没被删的自己变成了保留项」。
            kept = {row.items[p].path for p in row.keeps if p < row.count}
            row.items = [i for i in row.items if i.path not in gone]
            row.keeps = {p for p, i in enumerate(row.items) if i.path in kept}
            if not row.keeps and row.items:
                row.keeps = {0}          # 不变量 1：每组至少留一张
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
        """暂停/继续。工作线程在取任务前等 Event，所以暂停不占 CPU、不丢进度。"""
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
        except Exception as e:  # noqa: BLE001
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


# Qt 适配层在 model.make_list_model 里：GroupModel 刻意不继承
# QAbstractItemModel，这样模型层可以在没有 Qt 事件循环的环境里被单测。
