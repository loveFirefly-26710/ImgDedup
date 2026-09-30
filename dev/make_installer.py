"""一键产出两种发布形态。

    形态 A · 免安装版   dist/ImgDedup/ImgDedup.exe
             整个文件夹拷走就能跑，不写注册表、不进控制面板。
    形态 B · 安装版     dist/installer/ImgDedup-<版本>-setup.exe
             双击安装，建开始菜单/桌面快捷方式，可在"应用和功能"里卸载。

用法：
    python -m dev.make_installer --build      # 两个形态都出（先免安装版，再安装包）
    python -m dev.make_installer --portable   # 只出免安装版，不编安装包
    python -m dev.make_installer              # 只做安装包（要求 dist/ImgDedup 已存在）
    python -m dev.make_installer --check      # 只做产物体检，不重新编译

`--portable` 这条路是给"没装 Inno Setup 也要验证免安装版"用的：
它之后的体检只看免安装版那一半，不会因为"安装包不存在"判失败。

打包前必须确认没有进程占用 `dist/ImgDedup/ImgDedup.exe`，
否则 PyInstaller 的 COLLECT 步骤会因为文件锁失败，或者更糟：
产出一个混合了新旧文件的目录（我们踩过）。
"""

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

#: Inno Setup 6 的编译器。winget/choco 装的位置各不一样，按可能性找。
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
    """版本号单一来源：`imgdedup_core/__init__.py` 的 `__version__`。

    不在这里另起一个常量：两处版本号一定会有一天对不上，
    而安装包的版本号是要写进"应用和功能"列表给用户看的。
    """
    text = (PROJECT_ROOT / "imgdedup_core" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("在 imgdedup_core/__init__.py 里找不到 __version__")
    return m.group(1)


def exe_lockers() -> list[str]:
    """返回正在占用产物 exe 的进程名（空列表 = 没占用）。"""
    exe = ONEDIR / "ImgDedup.exe"
    if not exe.is_file():
        return []
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ImgDedup.exe"],
                             capture_output=True, text=True, errors="replace")
    except Exception:  # noqa: BLE001
        return []
    if "ImgDedup.exe" in out.stdout and "信息: 没有运行的任务" not in out.stdout:
        return ["ImgDedup.exe"]
    return []


def ensure_extra_dir() -> Path:
    """在免安装版里建出 `<exe 同级>/extra/`，并放一份说明。

    为什么不能用 PyInstaller 的 `datas`：`datas` 全部落进 `_internal/`，
    而 cv2 必须放在和 exe 同级的 `extra/`（`_internal/` 是打包器私有目录，
    用户不该往里翻，升级时也会被覆盖）。所以只能打包后自己建。

    放一份 README 是为了让"该往哪儿放"这件事在文件系统里就能看到，
    不用去翻文档：用户打开文件夹就明白了。
    """
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
        # `newline=""` 不能省：串里已经手写了 `\r\n`，而 `write_text`
        # 默认还会把 `\n` 再转一次（Windows 上转成 `\r\n`）——
        # 结果是 `\r\r\n`，用记事本打开每行都多一个空行。
        # （这个 bug 原来就有，实测产物里数出 33 个裸 CR 才发现的。）
        encoding="utf-8", newline="")
    return extra


def build_onedir() -> None:
    """跑 PyInstaller 重打免安装版（带图标），并补建 extra/ 目录。"""
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
    # 必须重新建（--clean 会重写 dist/ImgDedup，extra/ 会被清掉）
    ensure_extra_dir()
    print("      OK")


def build_installer() -> Path:
    """编安装包。返回产物路径。"""
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
    # 一律传绝对路径。让 .iss 用 `..\` 相对路径的话，Inno 在不同编译
    # 阶段的 cwd 不一致，会把 OutputDir 解析到错误的层级（实测少了一层目录）。
    defines = [
        f"/DAppVersion={ver}",
        f"/DSourceDir={ONEDIR}",
        f"/DIconFile={PROJECT_ROOT / 'resources' / 'icon.ico'}",
        f"/DOutDir={INSTALLER_DIR}",
    ]
    r = subprocess.run([str(iscc)] + defines + [str(ISS)],
                       cwd=str(PROJECT_ROOT), capture_output=True,
                       text=True, errors="replace")
    # Inno 的日志很长，只在失败时打全
    if r.returncode != 0:
        print(r.stdout[-4000:])
        print(r.stderr[-2000:])
        raise SystemExit(f"ISCC 失败，退出码 {r.returncode}")

    out = INSTALLER_DIR / f"ImgDedup-{ver}-setup.exe"
    if not out.is_file():
        # 找不到时列目录，方便看实际产物名
        listing = "\n".join(p.name for p in INSTALLER_DIR.iterdir())
        raise SystemExit(f"没找到预期产物 {out}\n目录内容：\n{listing}")
    print(f"      OK  {out.relative_to(PROJECT_ROOT)}  "
          f"({out.stat().st_size / 1024 / 1024:.1f} MB)")
    return out


def check(*, portable_only: bool = False) -> int:
    """产物体检：两种形态都在、图标都嵌进去了、体积合理。

    `portable_only` 给 `--portable` 用：那条路只打免安装版，安装包本来就不存在，
    不该因此判 FAIL。免安装版那一半与 `_report_external_cv2()` 两条路都要跑。
    """
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
        # version() 只在这条路里用得上，所以放在分支内：
        # `--portable` 时没必要去读 imgdedup_core/__init__.py。
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
    """报告 cv2 这个外置组件的状态。

    没装不算失败：它现在就是"按设计不随包分发"的可选组件。
    但两件事必须报出来，否则体检会给出误导性的"PASS"：
      · 包里有没有误把 cv2 打回去（那样就白省了 86 MB）；
      · `extra/` 目录建出来没有（用户得有地方放）。
    """
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
    """读 exe 里嵌的图标，返回一句描述；没有图标返回 None。

    直接问 Windows 自己（`LoadLibraryExW` + `EnumResourceNamesW`），
    而不是自己解 PE 字节：
      · 自研解析要处理 DOS 头 → PE 头 → 节表 → `.rsrc` → 三层资源目录，
        任何一步偏移算错都会得出"没有图标"的假阴性
        （本函数第一版就是这么错的：明明嵌进去了却报 None）。
      · Windows API 的语义就是"资源管理器看到什么"，所见即所得。

    用 `LOAD_LIBRARY_AS_DATAFILE` 加载：只映射文件、不执行它的入口点，
    所以对刚打好、还没验证过的 exe 也是安全的。
    """
    if not exe.is_file():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.LoadLibraryExW.restype = ctypes.c_void_p
        k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                       wintypes.DWORD]
        h = k32.LoadLibraryExW(str(exe), None, 0x2)  # AS_DATAFILE
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

            def cb(hmod, typ, name, lp):  # noqa: ANN001
                found.append(name)
                return True

            k32.EnumResourceNamesW(ctypes.c_void_p(h),
                                   ctypes.c_void_p(res_type),
                                   ENUMPROC(cb), None)
            return found

        groups = names(14)   # RT_GROUP_ICON
        images = names(3)    # RT_ICON（单个尺寸的位图）
        if not groups:
            return None
        return (f"图标组 {len(groups)} 个、内含 {len(images)} 个尺寸"
                if images else f"图标组 {len(groups)} 个")
    except Exception:  # noqa: BLE001
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
        # 体检的结论必须变成退出码。早先这里写死 `return 0`，
        # release.yml 那一步照样绿，坏掉的安装包会被直接发到 Release 页。
        return check(portable_only=True)

    if args.build:
        build_onedir()
    build_installer()
    print()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
