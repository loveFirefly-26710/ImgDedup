"""产出免安装版与 Inno Setup 安装包。"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
ONEDIR = DIST_DIR / "ImgDedup"
INSTALLER_DIR = DIST_DIR / "installer"
SPEC = PROJECT_ROOT / "imgdedup.spec"
ISS = PROJECT_ROOT / "imgdedup.iss"

ISCC_CANDIDATES = (
    Path(r"C:\Users\lightning\AppData\Local\Programs\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
)


def find_iscc() -> Path | None:
    for c in ISCC_CANDIDATES:
        if c.is_file():
            return c
    found = shutil.which("ISCC") or shutil.which("iscc")
    return Path(found) if found else None


def version() -> str:
    text = (PROJECT_ROOT / "imgdedup_core" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("在 imgdedup_core/__init__.py 里找不到 __version__")
    return m.group(1)


def exe_lockers() -> list[str]:
    exe = ONEDIR / "ImgDedup.exe"
    if not exe.is_file():
        return []
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ImgDedup.exe"],
                             capture_output=True, text=True, errors="replace")
    except Exception:
        return []
    if "ImgDedup.exe" in out.stdout and "信息: 没有运行的任务" not in out.stdout:
        return ["ImgDedup.exe"]
    return []


def ensure_extra_dir() -> Path:
    extra = ONEDIR / "extra"
    extra.mkdir(parents=True, exist_ok=True)
    readme = extra / "外置组件放这里.txt"
    readme.write_text(
        "这个文件夹用来放「外置组件」。\r\n"
        "两个大件都不随安装包分发，都是单独下载的；\r\n"
        "放好之后重新打开 ImgDedup.exe 即可。\r\n"
        "\r\n"
        "  1) cv2（图像处理库，86 MB）—— 扫描 / 去重必需\r\n"
        "\r\n"
        "     extra\\cv2\\\r\n"
        "         __init__.py\r\n"
        "         cv2.pyd\r\n"
        "         config.py\r\n"
        "\r\n"
        "  2) CLIP 模型（85 MB）—— 只有「深度」模式需要\r\n"
        "\r\n"
        "     extra\\models\\Xenova__clip-vit-base-patch32\\\r\n"
        "         vision_model.onnx\r\n"
        "         preprocessor_config.json\r\n"
        "         config.json\r\n"
        "\r\n"
        "怎么拿到：\r\n"
        "  1. 打开 ImgDedup.exe —— 缺 cv2 或模型时会弹提示，\r\n"
        "     对话框里有「自动下载」和「打开该文件夹」两个按钮；\r\n"
        "  2. 或找提供软件的人要 cv2-win64.zip / models-*.zip，\r\n"
        "     解压后把里面的 cv2 / models 文件夹整个拷进来。\r\n"
        "\r\n"
        "卸载时这个 extra\\ 目录会被一起删掉（避免留下孤儿文件），\r\n"
        "   想留着的话卸载前先把它们拷到别处。\r\n",
        encoding="utf-8", newline="")
    return extra


def build_onedir() -> None:
    print("[1/2] 打免安装版 (PyInstaller onedir) ...")
    lockers = exe_lockers()
    if lockers:
        raise SystemExit(
            f"以下进程正占用产物，先关掉再打包：{', '.join(lockers)}\n"
            f"（占用时 COLLECT 可能产出新旧混合的目录）")
    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean"],
        cwd=str(PROJECT_ROOT))
    if r.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {r.returncode}")
    ensure_extra_dir()
    print("      OK")


def build_installer() -> Path:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit(
            "找不到 Inno Setup 的 ISCC.exe。\n"
            "装一个：winget install --id JRSoftware.InnoSetup\n"
            "（或 choco install innosetup）")

    if not (ONEDIR / "ImgDedup.exe").is_file():
        raise SystemExit(
            f"免安装版不存在：{ONEDIR / 'ImgDedup.exe'}\n"
            f"先跑： python -m dev.make_installer --build")

    ver = version()
    print(f"[2/2] 打安装包 (Inno Setup)  版本 {ver} ...")
    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    defines = [
        f"/DAppVersion={ver}",
        f"/DSourceDir={ONEDIR}",
        f"/DIconFile={PROJECT_ROOT / 'resources' / 'icon.ico'}",
        f"/DOutDir={INSTALLER_DIR}",
    ]
    r = subprocess.run([str(iscc)] + defines + [str(ISS)],
                       cwd=str(PROJECT_ROOT), capture_output=True,
                       text=True, errors="replace")
    if r.returncode != 0:
        print(r.stdout[-4000:])
        print(r.stderr[-2000:])
        raise SystemExit(f"ISCC 失败，退出码 {r.returncode}")

    out = INSTALLER_DIR / f"ImgDedup-{ver}-setup.exe"
    if not out.is_file():
        listing = "\n".join(p.name for p in INSTALLER_DIR.iterdir())
        raise SystemExit(f"没找到预期产物 {out}\n目录内容：\n{listing}")
    print(f"      OK  {out.relative_to(PROJECT_ROOT)}  "
          f"({out.stat().st_size / 1024 / 1024:.1f} MB)")
    return out


def check(*, portable_only: bool = False) -> int:
    print("=" * 66)
    print("  发布产物体检")
    print("=" * 66)
    ok = True

    exe = ONEDIR / "ImgDedup.exe"
    if exe.is_file():
        mb = exe.stat().st_size / 1024 / 1024
        print(f"  免安装版主程序 : {mb:.1f} MB  {exe.relative_to(PROJECT_ROOT)}")
        ico = read_exe_icon_id(exe)
        if ico:
            print(f"    嵌入图标     : OK（{ico}）")
        else:
            print("    嵌入图标     : ❌ 没读到图标资源")
            ok = False
    else:
        print("  免安装版主程序 : ❌ 不存在")
        ok = False

    if portable_only:
        print("  安装包         : 本次没打（--portable 只出免安装版）")
    else:
        ver = version()
        setup = INSTALLER_DIR / f"ImgDedup-{ver}-setup.exe"
        if setup.is_file():
            mb = setup.stat().st_size / 1024 / 1024
            print(f"  安装包         : {mb:.1f} MB  "
                  f"{setup.relative_to(PROJECT_ROOT)}")
            ico = read_exe_icon_id(setup)
            if ico:
                print(f"    嵌入图标     : OK（{ico}）")
            else:
                print("    嵌入图标     : ❌ 没读到图标资源")
                ok = False
        else:
            print("  安装包         : ❌ 不存在")
            ok = False

    ok = _report_external_cv2(ok)

    print(f"\n  结论：{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _report_external_cv2(ok: bool) -> bool:
    M = 1024 * 1024
    bundled = [p for p in (ONEDIR / "_internal").rglob("*")
               if p.is_file() and p.name.lower() == "cv2.pyd"]
    if bundled:
        print(f"  外置组件 cv2 : ❌ 又被打回包里了"
              f"（{bundled[0].stat().st_size / M:.1f} MB）—— 白省了")
        ok = False
    else:
        print("  外置组件 cv2   : OK（未随包分发）")

    extra = ONEDIR / "extra"
    if extra.is_dir():
        has = (extra / "cv2" / "cv2.pyd").is_file()
        print(f"    摆放位置     : {extra.relative_to(PROJECT_ROOT)}"
              f"  {'（已放入 cv2）' if has else '（待用户放入 cv2）'}")
    else:
        print("    摆放位置     : ❌ extra/ 没建出来，用户不知道往哪儿放")
        ok = False

    total = sum(p.stat().st_size for p in ONEDIR.rglob("*") if p.is_file())
    print(f"    免安装版总体积: {total / M:.1f} MB"
          f"  （含 cv2 时约 {total / M + 86:.0f} MB）")
    return ok


def read_exe_icon_id(exe: Path) -> str | None:
    if not exe.is_file():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.LoadLibraryExW.restype = ctypes.c_void_p
        k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                       wintypes.DWORD]
        h = k32.LoadLibraryExW(str(exe), None, 0x2)
        if not h:
            return None

        k32.EnumResourceNamesW.restype = wintypes.BOOL
        k32.EnumResourceNamesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p]
        ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p)

        def names(res_type: int) -> list:
            found: list = []

            def cb(hmod, typ, name, lp):
                found.append(name)
                return True

            k32.EnumResourceNamesW(ctypes.c_void_p(h),
                                   ctypes.c_void_p(res_type),
                                   ENUMPROC(cb), None)
            return found

        groups = names(14)
        images = names(3)
        if not groups:
            return None
        return (f"图标组 {len(groups)} 个、内含 {len(images)} 个尺寸"
                if images else f"图标组 {len(groups)} 个")
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="产出免安装版 + 安装包")
    how = ap.add_mutually_exclusive_group()
    how.add_argument("--build", action="store_true",
                     help="两个形态都出：先重打免安装版，再编安装包")
    how.add_argument("--portable", action="store_true",
                     help="只出免安装版，不编安装包")
    ap.add_argument("--check", action="store_true",
                    help="只体检已有产物，不重新编译")
    args = ap.parse_args(argv)

    if args.check:
        return check()

    if args.portable:
        build_onedir()
        print()
        return check(portable_only=True)

    if args.build:
        build_onedir()
    build_installer()
    print()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
