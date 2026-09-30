"""GUI 入口：`python -m imgdedup_ui`

启动顺序刻意做成「先画窗口、后干重活」：
  1. 只导入 PySide6 与界面模块（不碰 imgdedup_core）；
  2. 立刻 show()，让首帧落地；
  3. 其余一切（扫描、缩略图、模型加载）都发生在用户动作之后或后台线程里。

另外支持 `--selftest`：不建窗口，只验证打包关键项（模型是否随包、能否离线推理、
核心与界面能否导入）。窗口版 exe 没有 stdout，所以它的判据是
「退出码 + `IMGDEDUP_SELFTEST_OUT` 指向的结果文件」，而不是控制台输出。
"""

from __future__ import annotations

import multiprocessing
import os
import sys

# QSS 已迁到 `theme.py`（流萤主题，5 套预设）。见 docs/流萤主题美化设计方案.md。
# 这里刻意不再内联样式：主题要支持运行时可切换，样式必须是"参数化的"。


def _force_utf8() -> None:
    """冻结后控制台可能是 GBK，打印中文/符号会 UnicodeEncodeError 并让进程带错退出。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


# 图标相关的两个函数不在这里实现：入口脚本不会被 PyInstaller 收进
# PYZ，放在这里的任何符号 `dev/check_frozen.py` 都扫不到（它只扫 PYZ）。
# 所以真身在 `imgdedup_ui/_entry_symbols.py`（普通模块，会被归档收录），
# 这里只做转发。见那个模块的 docstring。
from imgdedup_ui._entry_symbols import icon_path, set_app_icon  # noqa: E402,F401


# 外置组件 cv2 的检查与自动下载不在这里实现：入口脚本冻结后
# 不可导入，探针进不去，这条链路就没法在冻结产物里验证。
# 真身在 `imgdedup_ui/_entry_symbols.py`（普通模块，会被收进 PYZ）。
from imgdedup_ui._entry_symbols import _require_cv2  # noqa: E402,F401


def run_selftest() -> int:
    import json

    _force_utf8()
    lines: list[str] = []
    ok = True

    def log(msg: str) -> None:
        lines.append(msg)
        print(msg, flush=True)

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            good = fn()
        except Exception as e:  # noqa: BLE001
            log(f"  {label:<22} 异常 {type(e).__name__}: {e}")
            ok = False
            return
        log(f"  {label:<22} {'OK' if good else 'FAIL'}")
        ok = ok and bool(good)

    log("=" * 62)
    log("  ImgDedup 打包自检")
    log("=" * 62)
    log(f"  frozen     : {getattr(sys, 'frozen', False)}")
    log(f"  _MEIPASS   : {getattr(sys, '_MEIPASS', None)}")
    log(f"  executable : {sys.executable}")

    def _model():
        """CLIP 模型：外置组件，和 cv2 一样单独放置/下载。

        判据同 cv2 的三种分法：
          · 没放 → 记一条说明，不算失败（快速/均衡模式不需要它）；
          · 放了 → 必须真的能加载（在 `离线推理` 那条里实跑）；
          · 放了但坏 → 失败。
        """
        from imgdedup_core import embed as EMB

        log(f"  推荐位置    : {EMB.model_extra_dir()}")
        log(f"  解析结果    : {EMB.model_dir()}")
        if not EMB.is_ready():
            log("  模型未安装（可选外置组件）")
            log("      注意：没有它「深度」模式会自动跳过语义层，"
                "其余模式不受影响。")
            return True
        log(f"  模型来源    : "
            f"{'随程序目录' if EMB.is_bundled() else '本地缓存'}")
        return True

    check("CLIP 模型（外置）", _model)

    def _cv2():
        """外置组件 cv2：必须单独判定，不能混进"底层库加载"里。

        判据分三种，缺一不可：
          · 没装 → 记一条说明，不算失败（它现在是可选外置组件）；
          · 装了 → 必须真的能跑一次运算，光"import 成功"不算
            （曾经出现过"位置放对了但加载失败"的情况）；
          · 装了但坏 → 失败（这才是真正要拦住的情况）。
        """
        from imgdedup_core import opencv as OPENCV

        if not OPENCV.is_available():
            log(f"  cv2 未安装（可选外置组件）—— 应放到 {OPENCV.cv2_dir()}")
            log("      注意：没有它无法执行扫描/去重，只能浏览界面。")
            return True
        import numpy as np

        img = np.zeros((8, 8, 3), dtype=np.uint8)
        r = OPENCV.lazy_cv2.resize(img, (4, 4),
                                   interpolation=OPENCV.lazy_cv2.INTER_AREA)
        if tuple(r.shape) != (4, 4, 3):
            return False
        log(f"  cv2 {OPENCV.version()}（外置）{OPENCV.found_at()}")
        return True

    check("cv2 外置组件", _cv2)

    def _infer():
        import numpy as np

        from imgdedup_core import embed as EMB
        from imgdedup_core import opencv as OPENCV

        # 推理要过 cv2 的缩放，缺组件时跳过而不是判失败。
        # 否则"按设计没装可选组件"会被报成"包坏了"，把真正的失败淹掉。
        if not OPENCV.is_available():
            log("  跳过：cv2 未安装（推理链路需要它）")
            return True
        # 模型同理：它现在也是外置可选组件，缺了不算包坏。
        if not EMB.is_ready():
            log("  跳过：CLIP 模型未安装（外置组件）")
            return True

        enc = EMB.ClipEncoder()
        if not enc.load():
            return False
        r = EMB.encode_all(enc, [np.zeros((224, 224, 3), np.uint8)], batch=1)
        log(f"  推理维度    : {r.dim}")
        return r.ok

    check("离线推理", _infer)

    def _libs():
        # 只 import 就能证明 DLL 真的能加载：缺 DLL 会在 import 阶段就炸。
        # 这是二进制裁剪后最该守住的一条：裁错了 Qt6Quick/ffmpeg 没事，
        # 裁错了 libvips 就是整个功能不可用。
        #
        # cv2 不在这里：它是外置可选组件，由上面那条 `cv2 外置组件`
        # 单独判定（"没装"和"装了但坏"要分开处理，混在一起会把
        # "按设计没装"误报成"包坏了"）。
        import numpy
        import onnxruntime
        import PIL
        import pyvips

        # PDF / AI 后端。它是随包的，而 `pdfium.dll` 在包目录里、靠
        # `__file__` 相对定位：打包漏收那个 dll 时，"import 看着没问题、
        # 一 render 就炸"。所以这里不光 import，还真建一次文档证明能用。
        from imgdedup_core import decode as _decode

        _pdf = _decode.BACKENDS.pdfium
        if _pdf is None:
            raise RuntimeError(
                "pypdfium2 没加载起来："
                + str(_decode.BACKENDS.errors.get("pypdfium2", "?")))
        _doc = _pdf.PdfDocument.new()      # 不碰文件，只证明库真能干活
        _doc.close()

        log(f"  pypdfium2 {_decode.BACKENDS.summary().get('pypdfium2', '?')}"
            f"（PDF / AI 后端，随包）")
        log(f"  numpy {numpy.__version__} / Pillow {PIL.__version__} / "
            f"onnxruntime {onnxruntime.__version__}")
        log(f"  libvips {pyvips.version(0)}.{pyvips.version(1)}."
            f"{pyvips.version(2)}")
        return True

    check("底层库加载", _libs)

    def _core():
        from imgdedup_core import action, cache, feature, group, scan  # noqa: F401

        return True

    check("核心引擎导入", _core)

    def _ui():
        from imgdedup_ui.window import MainWindow  # noqa: F401

        return True

    check("界面模块导入", _ui)

    def _window():
        # 真正建一次窗口，能抓住被裁掉的 Qt DLL：只 import 是抓不到的。
        from PySide6.QtWidgets import QApplication

        from imgdedup_ui.window import MainWindow

        app = QApplication.instance() or QApplication([])
        win = MainWindow()
        win.resize(960, 640)
        win.show()
        app.processEvents()
        rows = win.view.model().rowCount()
        log(f"  窗口行数    : {rows}")
        win.close()
        return True

    check("界面可实例化", _window)

    out = os.environ.get("IMGDEDUP_SELFTEST_OUT")
    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"ok": ok, "lines": lines}, fh, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass
    log("")
    log("  自检通过 ✅" if ok else "  自检失败 ❌")
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--selftest" in argv:
        return run_selftest()
    if "--interact-test" in argv:
        # 把窗口塞满假数据 + 投递真实鼠标事件。
        # `--selftest` 只能证明"链路能跑通"（它只建一次窗口、行数为 0），
        # 证明不了"点得动、筛得对"。这个模式补上那一段。
        from dev.click_frozen import main as interact_main

        return interact_main()
    if "--probe-script" in argv:
        # 在冻结产物里跑一段任意探针脚本。
        #
        # 为什么需要：`--selftest` / `--interact-test` 都是写死的，
        # 新增的功能（预览缩放、动图、三档模式）没法验证"打包后还能用"。
        # 而"归档里有这个符号"（`dev/check_frozen.py`）只证明代码在包里，
        # 证明不了它真的能跑起来：冻结环境缺 DLL / 少数据文件是常事。
        #
        # 用法： `ImgDedup.exe --probe-script path\to\probe.py`
        # 探针脚本自己 `print`，结果由调用方按退出码判断。
        i = argv.index("--probe-script")
        if i + 1 >= len(argv):
            print("用法： --probe-script <脚本路径>")
            return 2
        script = argv[i + 1]
        import runpy

        runpy.run_path(script, run_name="__main__")
        return 0

    from PySide6.QtWidgets import QApplication

    from imgdedup_ui import theme
    from imgdedup_ui.window import MainWindow

    app = QApplication(argv)
    app.setApplicationName("ImgDedup")
    # 图标：窗口/任务栏/Alt+Tab 都要它。放在主题之前无所谓，
    # 但必须在建窗口之前：窗口一 show 就会去取应用图标。
    set_app_icon(app)
    # 外置组件（cv2）缺失时先说清楚再退，别让用户点进去才发现用不了。
    if not _require_cv2(app):
        return 3
    # 主题必须在建窗口之前套上：窗口里有些控件（品牌圆点、进度条）
    # 在构造期就会读配色常量，晚了它们会带着旧色显示一帧。
    theme.apply_theme(app, theme.load_theme_key())

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
