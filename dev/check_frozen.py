"""归档级校验：确认冻结产物里装的是本次的代码，不是上一次的旧包。

用法：
    python dev/check_frozen.py [exe路径] [符号1 符号2 ...]

不给符号时会用下面 `DEFAULT_SYMBOLS` 里那组（历次改动累积的关键符号）。
还可以用 `--absent 符号` 断言某个符号不该存在（用于确认旧实现被真正删除）。

为什么要做这一步：`--interact-test` 只能证明"这个包跑起来是对的"，
证明不了"这个包是我刚打的"。历史上真的出现过打包没生效、跑的是旧产物的情况。

归档读取的两个坑（都踩过）：
  1. `ZlibArchiveReader` 要的是文件路径 + 偏移，不能直接喂 code object，
     也不能对着字符串 "PYZ.pyz" 构造（`FileNotFoundError`）。
     正确姿势：`CArchiveReader(exe).extract("PYZ.pyz")` 拿到 bytes → 落临时文件。
  2. 手工解 PYZ 头（`PYZ\\0` + marshal）会 `bad marshal data`，别试。

方法名在 `co_names`，不在 `co_consts`。  `co_consts` 里只有字面量和
嵌套 code 对象，所以遍历时必须两者都看，且对 `co_consts` 里像 code 的递归下去。
第一版只查 `co_consts`，把 `ALL_KINDS`、`hit_test` 全判成 MISSING。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath("."))

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader  # noqa: E402

#: 历次改动累积的关键符号。加新功能时往这里追加，能防住"打包没生效"。
DEFAULT_SYMBOLS = [
    # M8 界面交互
    "hit_test", "ALL_KINDS", "_select_only", "_current_filter_key",
    # 流萤主题
    "apply_theme", "build_qss", "THEMES", "set_accent", "glow", "BrandDot",
    # 缩略图尺寸可调
    "set_thumb_size", "thumb_size", "sld_thumb", "_on_thumb_size_changed",
    "THUMB_MIN", "THUMB_MAX", "THUMB_DEFAULT", "_sync_view_width",
    # 预览框
    "PreviewPane", "show_path", "preview_cache", "PREVIEW_SIZE",
    # 本轮：预览与信息面板对调
    "right_split", "_build_right",
    # 滚轮减速（用户直接反馈的体验问题，必须确认进包）
    "WHEEL_STEP_PX", "WHEEL_NOTCH", "_on_wheel",
    # 配色深化：自绘的渐变背景 / 面板 / 品牌字
    "GradientPage", "set_stops", "PanelSurface", "BrandText",
    "_draw_card_shadow", "C_SHADOW", "C_EMBER", "C_ACCENT_DEEP",
    "BRAND_EMBER", "_sync_gradient", "paintEvent",
    # 本轮：右键打开文件夹（explorer 字符串命令行写法）
    "_reveal",
    # 本轮：预览滚轮缩放
    "wheelEvent", "zoom_by", "reset_zoom", "_clamp_pan", "_content_size",
    "ZOOM_MIN", "ZOOM_MAX", "ZOOM_STEP", "STILL_SIDE", "_paint_badges",
    "mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent",
    # 本轮：动图播放（回主线程必须走 Signal，不能用 QTimer.singleShot）
    "load_animation", "Animation", "is_animated", "_AnimDecodeTask",
    "_start_anim_if_needed", "_on_anim_ready", "_next_frame", "_stop_anim",
    "toggle_anim", "_schedule_next", "ANIM_FRAME_SIZE", "ANIM_MAX_FRAMES",
    "ANIM_MAX_BYTES", "ANIM_DEFAULT_MS", "ANIM_MIN_MS", "ANIM_MAX_MS",
    "_next_anim_token", "_pixmap_from_rgb",
    # 本轮：三档扫描模式（修掉"快速=精确"的真 bug）
    "MODES", "MODE_ORDER", "mode_at", "mode_info", "_sync_mode_hint",
    "lbl_mode", "cmb_mode",
    # 本轮：即时提示气泡（悬停/聚焦当场弹，且常驻不自动消失）
    "InstantToolTip", "_InstantToolTipFilter", "attach", "popup_for",
    "dismiss", "_relayout", "_place", "_mode_tip_text", "tip_mode",
    # 本轮：下拉框选中行改为自绘 delegate（QSS 上不了色，见 theme.py 注释）
    "ComboItemDelegate", "_apply_combo_style",
    "setItemDelegate", "tip_filter", "tip_thumb",
    # 本轮：图标 + 两种发布形态（安装包 / 免安装版）
    # 图标有「两处」：PE 资源段由 spec 的 EXE(icon=) 负责（打包期，不进
    # 归档，符号表看不到）；datas 里那份由运行时 `icon_path` 找出来。
    "icon_path", "set_app_icon", "setWindowIcon", "QIcon",
    # `SetCurrentProcessExplicitAppUserModelID` 是 `ctypes.windll.shell32`
    # 的属性链，Python 3.12+ 的 LOAD_ATTR 会把它整个记进 co_names；
    # 若不在此处出现说明该分支被删了（任务栏会退回 Python 图标）。
    "SetCurrentProcessExplicitAppUserModelID", "windll", "shell32",
    # 本轮：预览框的百分比输入 + 重置（`zoomChanged` 是两边唯一的同步通道）
    "zoomChanged", "set_zoom", "_build_zoom_row", "_sync_zoom_edit",
    "_apply_zoom_from_edit", "zoom_edit", "btn_zoom_reset", "editingFinished",
    "hasFocus", "tip_zoom",
    # 本轮：动图解码任务的生命周期（修"快速切 GIF → 卡住 → 闪退"）
    # 这几个是"资源回收"符号，少了不会功能报错、只会闪退，
    # 所以必须靠符号表 + 退出码守住（闪退没有回溯，最难查）。
    "shutdown_anim", "_park_anim_task", "_ANIM_PARKED", "isRunning",
    # 本轮：cv2 改成外置可选组件（不再随包分发）
    # 这几个是"外置组件的加载链路"，少了不会报错、只会静默用不上 cv2，
    # 所以必须靠符号表确认它们真的在包里。
    "lazy_cv2", "install_path_hook", "candidate_roots", "missing_hint",
    "cv2_dir", "extra_dir", "found_at", "refresh",
    # 外置组件的"自动下载"链路（对话框里的按钮走这条）
    # `wheel_matches` 是关键：它里面那段 abi3 匹配写错了的话，
    # 会"找不到任何可用版本"：功能完全不可用，但不会报错。
    "wheel_matches", "find_wheel", "extract_cv2", "should_skip",
    # 启动期逻辑（在 `_entry_symbols.py` 里，所以符号表能看到。
    # 放 `__main__.py` 的话这里永远报缺失，见那个模块的说明）
    "_require_cv2", "_download_cv2", "_Cv2DownloadTask", "wasCanceled",
    # `full_hash_skipped` / `perceptual_skipped` 不要加进来。
    # 它们是 `meta["..."]` 的字典 key 字面量，落在 `co_consts` 里，
    # 而本脚本只汇总 `co_names`，加了必然永久报缺失（上面第 51 行的说明）。
    # 要断言这两个标记真的写进了 meta，用 `test_preview_zoom_anim.py`
    # 里的端到端测试去查（那条真跑一遍 worker 并读 meta）。
    #
    # 同理别加 `C_TIP_BG` / `_DELEGATE_SEL_BG` 这类常量名：
    # 它们是模块级变量名，读取点会以 `co_names` 出现（`C_TIP_BG` 确实
    # 在 `InstantToolTip.paintEvent` 里被读，所以可用），但 `_DELEGATE_*`
    # 只在 `theme._apply_combo_style` 里以属性名出现一次：已由
    # `_apply_combo_style` 本身覆盖，不必重复列。
    #
    # PDF/AI 后端是 pypdfium2(BSD/Apache)，随包分发。
    # 旧的 `_meta_mupdf` / `_thumb_mupdf` / `MUPDF_EXTS` 已经不存在了，
    # 用 `--absent` 断言它们真的被删干净（见本脚本用法说明）。
    # `pdfium` 这个符号特别值得守：pypdfium2 是按 `__file__` 相对
    # 找 `pdfium.dll` 的，打包漏了 dll 时表现是"源码里好好的、
    # 冻结后一 import 就炸"。
    "PDF_EXTS", "_meta_pdfium", "_thumb_pdfium", "pdfium",
]
#: 别把 `t["bg_top"]` 这类字典 key 字面量写进符号表。
#: 它们落在 `co_consts` 里（当字面量），而本脚本只汇总 `co_names`
#: （函数名/属性名/变量名）。曾经加过 `bg_top`/`grad_a` 等 5 个 key，
#: 结果永远报"缺失"，反而掩盖了真正的漏打。要断言它们存在，
#: 就断言读它的那个方法名（如 `set_stops`、`_sync_gradient`）。


def collect_names(zr: ZlibArchiveReader) -> set[str]:
    """把归档里所有模块的 `co_names` 汇总成一个集合。"""
    found: set[str] = set()
    for name in zr.toc:
        try:
            mod = zr.extract(name)
        except Exception:  # noqa: BLE001
            continue
        if mod is None:
            continue
        stack = [mod]
        while stack:
            code = stack.pop()
            found.update(getattr(code, "co_names", ()))
            for const in getattr(code, "co_consts", ()):
                if hasattr(const, "co_names"):
                    stack.append(const)
    return found


def main(argv: list[str]) -> int:
    exe = "dist/ImgDedup/ImgDedup.exe"
    symbols = list(DEFAULT_SYMBOLS)
    absent: list[str] = []

    args = argv[1:]
    if args and not args[0].startswith("--"):
        exe = args.pop(0)
    while args:
        flag = args.pop(0)
        if flag == "--absent":
            absent.append(args.pop(0))
        else:
            symbols.append(flag)

    if not os.path.exists(exe):
        print(f"[失败] 找不到产物：{exe}")
        return 1

    # 把内嵌 PYZ 落到临时文件：ZlibArchiveReader 只认路径
    scratch = os.path.abspath(".scratch")
    os.makedirs(scratch, exist_ok=True)
    tmp = os.path.join(scratch, "_pyz_check.pyz")
    try:
        with open(tmp, "wb") as f:
            f.write(CArchiveReader(exe).extract("PYZ.pyz"))
        found = collect_names(ZlibArchiveReader(tmp))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    hit = [s for s in symbols if s in found]
    miss = [s for s in symbols if s not in found]

    print(f"归档级校验（{exe}）")
    print(f"  应有符号：{len(hit)}/{len(symbols)} 命中")
    if miss:
        print(f"  缺失：{miss}")
    if absent:
        leaked = [s for s in absent if s in found]
        print(f"  反向断言：{len(absent) - len(leaked)}/{len(absent)} 确认不存在")
        if leaked:
            print(f"  旧符号残留：{leaked}")

    ok = not miss and not [s for s in absent if s in found]
    print(f"  结论：{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
