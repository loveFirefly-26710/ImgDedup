"""归档级符号校验：读 exe 内嵌的 PYZ，逐项确认关键符号在包里。

`--selftest` 只能证明「这个包跑起来是对的」，证明不了「这个包是刚打的」。
这一步专门挡「改了源码、跑的是旧产物」这种情况。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath("."))

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

DEFAULT_SYMBOLS = [
    "hit_test", "ALL_KINDS", "_select_only", "_current_filter_key",
    "apply_theme", "build_qss", "THEMES", "set_accent", "glow", "BrandDot",
    "set_thumb_size", "thumb_size", "sld_thumb", "_on_thumb_size_changed",
    "THUMB_MIN", "THUMB_MAX", "THUMB_DEFAULT", "_sync_view_width",
    "PreviewPane", "show_path", "preview_cache", "PREVIEW_SIZE",
    "right_split", "_build_right",
    "WHEEL_STEP_PX", "WHEEL_NOTCH", "_on_wheel",
    "GradientPage", "set_stops", "PanelSurface", "BrandText",
    "_draw_card_shadow", "C_SHADOW", "C_EMBER", "C_ACCENT_DEEP",
    "BRAND_EMBER", "_sync_gradient", "paintEvent",
    "_reveal",
    "wheelEvent", "zoom_by", "reset_zoom", "_clamp_pan", "_content_size",
    "ZOOM_MIN", "ZOOM_MAX", "ZOOM_STEP", "STILL_SIDE", "_paint_badges",
    "mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent",
    "load_animation", "Animation", "is_animated", "_AnimDecodeTask",
    "_start_anim_if_needed", "_on_anim_ready", "_next_frame", "_stop_anim",
    "toggle_anim", "_schedule_next", "ANIM_FRAME_SIZE", "ANIM_MAX_FRAMES",
    "ANIM_MAX_BYTES", "ANIM_DEFAULT_MS", "ANIM_MIN_MS", "ANIM_MAX_MS",
    "_next_anim_token", "_pixmap_from_rgb",
    "MODES", "MODE_ORDER", "mode_at", "mode_info", "_sync_mode_hint",
    "lbl_mode", "cmb_mode",
    "InstantToolTip", "_InstantToolTipFilter", "attach", "popup_for",
    "dismiss", "_relayout", "_place", "_mode_tip_text", "tip_mode",
    "ComboItemDelegate", "_apply_combo_style",
    "setItemDelegate", "tip_filter", "tip_thumb",
    "icon_path", "set_app_icon", "setWindowIcon", "QIcon",
    "SetCurrentProcessExplicitAppUserModelID", "windll", "shell32",
    "zoomChanged", "set_zoom", "_build_zoom_row", "_sync_zoom_edit",
    "_apply_zoom_from_edit", "zoom_edit", "btn_zoom_reset", "editingFinished",
    "hasFocus", "tip_zoom",
    "shutdown_anim", "_park_anim_task", "_ANIM_PARKED", "isRunning",
    "lazy_cv2", "install_path_hook", "candidate_roots", "missing_hint",
    "cv2_dir", "extra_dir", "found_at", "refresh",
    "wheel_matches", "find_wheel", "extract_cv2", "should_skip",
    "_require_cv2", "_download_cv2", "_Cv2DownloadTask", "wasCanceled",
    "_meta_pdfium", "_thumb_pdfium", "pdfium",
]


def collect_names(zr: ZlibArchiveReader) -> set[str]:
    found: set[str] = set()
    for name in zr.toc:
        try:
            mod = zr.extract(name)
        except Exception:
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
