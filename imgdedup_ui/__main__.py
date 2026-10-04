"""程序入口，四种模式：

- 默认：起主窗口
- `--selftest`：自检（核心引擎 / 界面 / 底层库 / 外置组件能否加载）
- `--interact-test`：离屏投真实鼠标事件，做界面冒烟
- `--probe-script`：在冻结产物里跑一段探针脚本"""

from __future__ import annotations

import multiprocessing
import os
import sys



def _force_utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


from imgdedup_ui._entry_symbols import set_app_icon


from imgdedup_ui._entry_symbols import _require_cv2


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
        except Exception as e:
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

        if not OPENCV.is_available():
            log("  跳过：cv2 未安装（推理链路需要它）")
            return True
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
        import numpy
        import onnxruntime
        import PIL
        import pyvips

        from imgdedup_core import decode as _decode

        _pdf = _decode.BACKENDS.pdfium
        if _pdf is None:
            raise RuntimeError(
                "pypdfium2 没加载起来："
                + str(_decode.BACKENDS.errors.get("pypdfium2", "?")))
        _doc = _pdf.PdfDocument.new()
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
        from imgdedup_core import action, cache, feature, group, scan

        return True

    check("核心引擎导入", _core)

    def _ui():
        from imgdedup_ui.window import MainWindow

        return True

    check("界面模块导入", _ui)

    def _window():
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
        except Exception:
            pass
    log("")
    log("  自检通过 ✅" if ok else "  自检失败 ❌")
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--selftest" in argv:
        return run_selftest()
    if "--interact-test" in argv:
        from dev.click_frozen import main as interact_main

        return interact_main()
    if "--probe-script" in argv:
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
    set_app_icon(app)
    if not _require_cv2(app):
        return 3
    theme.apply_theme(app, theme.load_theme_key())

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
