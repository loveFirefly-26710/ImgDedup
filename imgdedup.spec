# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

核心约定：两个大件都不随包分发，改成外置组件（同一套 `extra/` 约定）：

| 组件 | 体积 | 用户摆放位置 |
|---|---|---|
| CLIP 模型 | ~85 MB | `<exe 同级>/extra/models/` |
| cv2 | ~86 MB | `<exe 同级>/extra/cv2/` |

两者都支持"程序内自动下载"，所以用户不放也能跑（只是首次要联网）。

把模型放进 `datas` 会让安装包膨胀到 220 MB+，而且和 cv2 的外置设计
自相矛盾。运行时的查找顺序见 `imgdedup_core.embed.bundled_root()`
（第一位就是 `extra/models`）。

注意：
  * `SPECPATH` 是 spec 所在目录（别当成它的父目录）：直接用 `Path(SPECPATH)`。
  * `excludes` 只过滤 Python 模块，DLL 要另用二进制名过滤。
  * 模型允许缺失：这里不要写"缺模型就 `raise SystemExit`"，
    否则 CI 上没取模型的构建会直接失败，缺模型是发布形态的正常状态。
  * 本文件是手写的项目文件，不是构建产物，所以 `dev/purge.py` 的
    `*.spec` 通配里有一条保留名单护着它。

用法：
    python -m PyInstaller imgdedup.spec --noconfirm --distpath dist --workpath build
"""

import os
from pathlib import Path

PROJ = Path(SPECPATH)

# ---- 入口：GUI 就绪前先用自检探针，避免打包链路无处可跑 ----
GUI_ENTRY = PROJ / "imgdedup_ui" / "__main__.py"
PROBE_ENTRY = PROJ / "dev" / "frozen_probe.py"
ENTRY = GUI_ENTRY if GUI_ENTRY.exists() else PROBE_ENTRY

# ---- 模型：不打包。留个常量只为日志/自检时报告它"本该在哪" ----
MODEL_DIR = PROJ / "resources" / "models"

datas = []

# ---- 应用图标 ----
# 图标源是 `resources/icon.ico`（多尺寸：256/128/64/48/32/24/16）。
# 由 `dev/make_icon.py` 从原图生成，随项目走，不依赖桌面路径。
#
# 这里要做两件互不替代的事：
#   1. `EXE(icon=...)`：把图标嵌进 exe 的 PE 资源段。
#      这管的是「文件在资源管理器里长什么样」「任务栏右键菜单」。
#   2. 把图标放进 `datas`：运行时 `QApplication.setWindowIcon()` 要读它。
#      这管的是「窗口左上角」「Alt+Tab 切换器」。
#   只做 1 会看到窗口图标还是 Qt 默认的羽毛；只做 2 则 exe 文件本身没图标。
ICON_ICO = PROJ / "resources" / "icon.ico"
ICON_PNG = PROJ / "resources" / "icon.png"
if not ICON_ICO.is_file():
    raise SystemExit(
        f"缺少图标 {ICON_ICO}\n"
        f"先跑： python -m dev.make_icon"
    )
datas.append((str(ICON_ICO), "resources"))
datas.append((str(ICON_PNG), "resources"))

# ---- pypdfium2：PDF / AI 解码后端（随包分发）----
# 底层是 PDFium（Chrome 的 PDF 引擎），许可宽松（BSD-3-Clause / Apache-2.0），
# 所以直接打进包：体积代价只有 8 MB，换来 PDF / AI 开箱可用。
#
# 下面两条都不能省，否则冻结后一 import 就炸：
#   1. `pypdfium2_raw/bindings.py` 是按 `Path(__file__).parent / "pdfium.dll"`
#      找 DLL 的。模块若只进 PYZ 归档，`__file__` 指向归档内部，
#      就找不到那个 dll，所以要让这两个包以真实 .py 文件落地
#      （见 Analysis 的 `module_collection_mode`）。
#   2. PyInstaller 没有 pypdfium2 的 hook，6.9 MB 的 `pdfium.dll`
#      属于包内数据文件，静态分析不会收集：必须显式 collect。
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas += collect_data_files("pypdfium2_raw")       # pdfium.dll + version.json
binaries = collect_dynamic_libs("pypdfium2_raw")   # 双保险：按二进制再收一遍

# ---- 排除项：只写确定不需要的 Python 模块 ----
excludes = [
    "tkinter", "test", "unittest", "pydoc_data",
    "matplotlib", "IPython", "jupyter", "notebook",
    "PyQt5", "PyQt6", "PySide2",
    # 本项目完全没用到、却被间接拖进来的科学计算栈（量下来白占 ~80 MB）
    "scipy", "pandas", "sympy", "h5py", "numba", "llvmlite",
    # cv2 是外置可选组件，不随包分发（cv2.pyd 一个文件 86 MB，
    # 占整个程序 21.7%）。运行时由 `imgdedup_core/opencv.py` 到
    # `<exe 同级>/extra/cv2/` 去找并挂进 sys.path。
    # 这里必须排除，否则静态分析会顺着 `opencv.py` 里那句 `import cv2`
    # 把 86 MB 又拖回来。
    "cv2",
]

a = Analysis(
    [str(ENTRY)],
    pathex=[str(PROJ)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # onnxruntime 的 CPU provider 在某些版本里是动态加载的
        "onnxruntime.capi._pybind_state",
        # `--interact-test` 用的验证模块。它是 `import dev.click_frozen` 里的
        # 动态导入，静态分析看不到：不加这条，冻结后该模式会 ImportError。
        "dev.click_frozen",
        # 外置 cv2 依赖它，必须显式收集。
        # `cv2/__init__.py` 第 12 行是 `import numpy.core.multiarray`
        # （numpy 1.x 时代的旧路径）。numpy 2.x 里 `numpy.core.multiarray`
        # 只是个转发到 `numpy._core.multiarray` 的小垫片，
        # 而我们自己的代码从来不用它，所以静态分析不会收集，
        # 冻结后那个垫片就没了，外置 cv2 一导入就
        # `ModuleNotFoundError: No module named 'numpy.core.multiarray'`。
        #
        # 这个坑很隐蔽：把 cv2 排除出包之前，正是"cv2 自己 import 了它"
        # 顺带把它带进来的，所以一排除 cv2，它就一起消失了，
        # 表现成"外置 cv2 放对了位置却还是加载失败"。
        "numpy.core.multiarray",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
    # pypdfium2 按 `__file__` 相对找 `pdfium.dll`，所以这两个包必须以
    # 真实 .py 文件落到 `_internal/` 下，不能只进 PYZ 归档。
    module_collection_mode={"pypdfium2": "py", "pypdfium2_raw": "py"},
)

# ---- 二进制裁剪 ----
# `excludes` 只过滤 Python 模块，DLL / pyd 必须另按文件名过滤。
# 否则 Qt6Qml / Quick / Designer、opengl32sw 之类照样被打进去：
# 不裁 227.5 MiB，裁完 190.5 MiB（差 37 MiB）。
# cv2 是外置组件，已由上面的 `excludes` 排除；下面的关键字再按文件名兜一道。
EXCLUDE_BINARIES_KEYWORDS = (
    "opencv_videoio_ffmpeg",   # cv2 的视频解码器：本工具只处理静态图
    # `excludes` 有时拦不住 cv2 的二进制（hook 会直接把 cv2.pyd 塞进来），
    # 所以这里再按文件名兜一道。cv2 是外置组件，见上面 `excludes` 的说明。
    "cv2.pyd", "cv2/", "cv2\\",
    "opengl32sw",              # Qt 的软件 OpenGL 回退，widget 应用用不到
    "Qt6Quick", "Qt6Qml", "Qt6QmlModels", "Qt6QmlWorkerScript",
    "Qt6Pdf", "Qt6Designer", "Qt6UiTools", "Qt6Test", "Qt6Charts",
    "Qt6DataVisualization", "Qt63D", "Qt6Multimedia", "Qt6WebEngine",
    "Qt6WebChannel", "Qt6WebSockets", "Qt6Bluetooth", "Qt6SerialPort",
    "Qt6Nfc", "Qt6Positioning", "Qt6Location", "Qt6Sql", "Qt6Help",
    "Qt6RemoteObjects", "Qt6Scxml", "Qt6Sensors", "Qt6TextToSpeech",
    "Qt6VirtualKeyboard", "Qt6ShaderTools", "Qt6StateMachine",
    "Qt6Concurrent", "Qt6HttpServer", "Qt6Labs",
)


def _keep_binary(entry) -> bool:
    name = os.path.basename(str(entry)).lower()
    return not any(k.lower() in name for k in EXCLUDE_BINARIES_KEYWORDS)


_before = len(a.binaries)
a.binaries = [b for b in a.binaries if _keep_binary(b[0])]
print(f"[spec] 二进制裁剪: {_before} -> {len(a.binaries)} 项")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ImgDedup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 嵌进 PE 资源段的图标（资源管理器 / 任务栏右键看到的就是它）
    icon=str(ICON_ICO),
    # 探针入口需要控制台才能看到自检输出；GUI 入口则不要控制台窗口
    console=not GUI_ENTRY.exists(),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ImgDedup",
)
