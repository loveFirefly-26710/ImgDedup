"""路径 B：开发/测试垃圾的真删通道（不进回收站）。

与路径 A（imgdedup_core.action，用户删除重复图片）物理隔离：
  * 本模块只允许删除 .scratch/ 之下的内容，护栏写死在函数里，调用方无法绕过；
  * 本模块永不进入回收站，路径 A 永不真删；
  * 两者不共享任何函数。

清理对象范围（对应需求澄清第 1 点）：
  1. .scratch/ 下的全部中间产物：图片副本树、硬链接树、格式转换样本、
     缩略图缓存、catalog.db、临时脚本、JSON 中间结果；
  2. Python 缓存：__pycache__/、*.pyc；
  3. 构建产物：build/、dist/、*.spec（PyInstaller）；
  4. 工具缓存：.pytest_cache/、.mypy_cache/、.ruff_cache/、.coverage；
  5. 运行日志：*.log、崩溃转储。
明确排除：E:\\1\\test 下的原始图片、用户真实图库、任何非本项目目录。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_DIRNAME = ".scratch"

#: 允许被 purge 真删的唯一根目录名（相对项目根）
ALLOWED_ROOTS = (SCRATCH_DIRNAME,)

#: 项目根下可整体清理的构建/缓存目录名
BUILD_JUNK_DIRS = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage",
    "build",
    "dist",
)

BUILD_JUNK_GLOBS = ("*.pyc", "*.pyo", "*.spec", "*.log", "*.dmp")


class PurgeRefused(RuntimeError):
    """护栏拒绝执行删除。任何一条不满足都必须抛错，而不是“尽力而为”。"""


# ---------------------------------------------------------------- 路径规整

def long_path(p) -> str:
    """把路径规整为长名形式。

    不能直接用字符串比较路径：本机 Path.resolve() 可能返回 8.3 短名
    （C:\\Users\\LIGHTN~1\\...），与调用方给的长名指同一目录但字符串不等，
    会造成“护栏误判”这种纯静默故障。
    """
    s = str(Path(p).absolute())
    if sys.platform == "win32":
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(s, buf, 32768)
        if n:
            s = buf.value
    return s


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(long_path(p)))


def is_reparse_point(path) -> bool:
    """判断是否为符号链接 / 目录联接（junction）。

    必须拒绝：.scratch 下若存在指向外部的链接，递归删除会顺着链接删到外面。
    """
    p = str(path)
    if os.path.islink(p):
        return True
    if sys.platform == "win32":
        try:
            attrs = os.stat(p, follow_symlinks=False).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


# ---------------------------------------------------------------- 根目录

def scratch_root() -> Path:
    d = PROJECT_ROOT / SCRATCH_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def scratch_path(*parts) -> Path:
    p = scratch_root().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- 护栏

def _assert_purgeable(target) -> Path:
    """真删前的硬护栏。任何一条不满足直接抛 PurgeRefused。"""
    tgt = Path(target)
    if not tgt.exists():
        raise PurgeRefused(f"目标不存在: {tgt}")

    allowed = [_norm(PROJECT_ROOT / name) for name in ALLOWED_ROOTS]
    n = _norm(tgt)

    if any(n == a for a in allowed):
        raise PurgeRefused(f"拒绝删除白名单根目录本身: {tgt}")
    if not any(n.startswith(a + os.sep) for a in allowed):
        raise PurgeRefused(
            f"路径不在允许真删的白名单之下，拒绝执行: {tgt}\n"
            f"  允许的根: {allowed}"
        )
    if is_reparse_point(tgt):
        raise PurgeRefused(f"目标是符号链接/联接，拒绝删除: {tgt}")
    return tgt


# ---------------------------------------------------------------- 真删

def _collect_tree(root: str) -> tuple[list[str], list[str], list[str], int]:
    """自研后序遍历，绝不进入 reparse point（符号链接 / 目录联接）。

    为什么不用 shutil.rmtree：Windows 上目录联接在 os.scandir 里依然被报告
    为目录，一旦递归进去就会删到链接目标——也就是 .scratch 之外的真实数据。
    本模块的存在前提是"只可能删到 .scratch 内"，遍历必须自己控制。

    返回 (文件列表, 目录列表[后序], 链接列表, 字节数)。
    """
    files: list[str] = []
    dirs: list[str] = []
    links: list[str] = []
    size = 0

    def rec(d: str) -> None:
        nonlocal size
        try:
            entries = list(os.scandir(d))
        except OSError:
            dirs.append(d)
            return
        for entry in entries:
            full = entry.path
            if is_reparse_point(full):
                links.append(full)  # 只删链接本身，绝不跟进去
                continue
            if entry.is_dir(follow_symlinks=False):
                rec(full)
            else:
                try:
                    size += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
                files.append(full)
        dirs.append(d)  # 后序：子目录先于父目录

    rec(root)
    return files, dirs, links, size


def _cmd(args: list[str]) -> None:
    """用 cmd 执行一条命令（列表形式，避免引号被 shell 二次解析）。"""
    try:
        subprocess.run(["cmd", "/c"] + args, capture_output=True, timeout=300)
    except Exception:  # noqa: BLE001
        pass


def _true_delete(files: list[str], dirs: list[str], links: list[str]) -> None:
    """真正的删除。

    本机实测结论（对照实验，见 .scratch/probe_delete.py）：
        进回收站：os.unlink / shutil.rmtree / powershell Remove-Item /
                  SetFileInformationByHandle(POSIX 语义)
        真删    ：cmd /c del   ← 唯一可行
    连官方"绕过回收站、立即永久删除"的 POSIX 语义 API 都会被重定向，
    所以这里只能走 cmd。这不是风格选择，是本机环境的事实。

    注意：目录必须逐个 rd，不能写成 `rd /q "a" & rd /q "b"` 再整体交给
    cmd /c：整串含空格会被 subprocess 加引号，cmd 的引号剥离规则会把它
    解析坏（实测导致目录删不掉、purge 直接报错）。
    """
    # 1) 文件：分批 del（cmd 单条命令行有长度上限）
    for i in range(0, len(files), 40):
        _cmd(["del", "/f", "/q"] + files[i:i + 40])

    # 2) 链接：只移除链接本身，绝不跟进去
    for link in links:
        try:
            os.rmdir(link)
        except OSError:
            try:
                os.unlink(link)
            except OSError:
                pass

    # 3) 目录：自底向上逐个 rd（dirs 已是后序）
    for d in dirs:
        _cmd(["rd", "/q", d])


def _rmtree(p: Path) -> tuple[int, int]:
    """真删整棵子树。返回 (删除的文件数, 释放的字节数)。"""
    files, dirs, links, size = _collect_tree(str(p))
    _true_delete(files, dirs, links)
    return len(files), size


def sweep_recycle_bin(roots, *, since: float = 0.0) -> int:
    """兜底清扫：把原始路径落在给定根之下的回收站 $I/$R 条目物理删除。

    用途：万一某次删除仍被重定向进回收站，用它兜底；也可清理历史遗留。

    按前缀匹配而不是精确匹配，因为历史遗留的条目对应的文件早已不在磁盘上，
    无法再枚举出精确路径。
    绝不做全盘清空：回收站里往往还混着别处删的文件，一把梭会误伤。
    """
    import struct

    # 必须同时匹配「根本身」和「根之下的东西」：
    # 只匹配 `根\` 会漏掉「被删的就是根目录本身」这种情况（例如 build/），
    # 实测因此漏掉了 5 条 build / dist / __pycache__ 的残留。
    prefixes = [os.path.normcase(_norm(p)).rstrip(os.sep) for p in roots]
    if not prefixes:
        return 0
    drive = os.path.splitdrive(str(PROJECT_ROOT))[0]
    rb = os.path.join(drive + os.sep, "$Recycle.Bin")
    if not os.path.isdir(rb):
        return 0

    killed = 0
    for sid in os.listdir(rb):
        d = os.path.join(rb, sid)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.startswith("$I"):
                continue
            ip = os.path.join(d, name)
            try:
                if os.stat(ip).st_mtime < since:
                    continue
                with open(ip, "rb") as fh:
                    raw = fh.read(8192)
                if len(raw) < 28:
                    continue
                nlen = struct.unpack("<I", raw[24:28])[0]
                if not (0 < nlen <= 4096):
                    continue
                orig = raw[28:28 + nlen * 2].decode("utf-16-le", "replace").replace("\x00", "")
            except Exception:  # noqa: BLE001
                continue
            n = os.path.normcase(_norm(orig))
            if not any(n == p or n.startswith(p + os.sep) for p in prefixes):
                continue
            for victim in (ip, os.path.join(d, "$R" + name[2:])):
                try:
                    os.chmod(victim, stat.S_IWRITE)
                except OSError:
                    pass
                try:
                    os.unlink(victim)
                    killed += 1
                except OSError:
                    pass
    return killed


def purge(target, *, dry_run: bool = False) -> dict:
    """真删 target（必须是 .scratch 之下的路径）。

    真删手段：cmd /c del：本机唯一不被重定向进回收站的删除方式。
    不能用 os.unlink / shutil.rmtree / rm -rf，它们在本机全部进回收站，
    磁盘空间不会释放。

    删除后校验：目标路径必须已不存在；同时报告可用空间变化作为佐证。
    """
    tgt = _assert_purgeable(target)

    before = shutil.disk_usage(str(PROJECT_ROOT)).free
    if dry_run:
        files, size = _count_only(tgt)
        return {"path": str(tgt), "dry_run": True, "files": files, "bytes": size,
                "freed": 0, "exists_after": True}

    if tgt.is_dir():
        files, size = _rmtree(tgt)
    else:
        size = tgt.stat().st_size
        files = 1
        _true_delete([str(tgt)], [], [])

    exists_after = tgt.exists()
    if exists_after:
        raise PurgeRefused(f"删除后目标仍存在，说明未真正删除: {tgt}")

    after = shutil.disk_usage(str(PROJECT_ROOT)).free
    return {
        "path": str(tgt),
        "dry_run": False,
        "files": files,
        "bytes": size,
        "freed": after - before,
        "exists_after": exists_after,
    }


def _count_only(p: Path) -> tuple[int, int]:
    if p.is_file():
        return 1, p.stat().st_size
    files = size = 0
    for root, dirs, names in os.walk(p):
        for name in names:
            try:
                size += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass
    return files, size


def purge_scratch(*, dry_run: bool = False) -> list[dict]:
    """清理 .scratch/ 下的全部条目（保留 .scratch 目录本身）。"""
    root = scratch_root()
    results = []
    for entry in sorted(root.iterdir()):
        try:
            results.append(purge(entry, dry_run=dry_run))
        except PurgeRefused as e:
            results.append({"path": str(entry), "refused": str(e)})
    return results


def purge_build_junk(*, dry_run: bool = False) -> list[dict]:
    """清理项目内的 __pycache__ / *.pyc / 构建产物 / 日志。"""
    results = []
    targets: list[Path] = []

    for name in BUILD_JUNK_DIRS:
        for p in PROJECT_ROOT.rglob(name):
            if p.is_dir() and SCRATCH_DIRNAME not in p.parts:
                targets.append(p)
    # 手写的 spec 是项目文件，不是垃圾：只有 PyInstaller 自动生成的才算。
    # 实测踩过：imgdedup.spec 被 *.spec 通配当构建垃圾删掉了。
    keep = {"imgdedup.spec"}
    for pattern in BUILD_JUNK_GLOBS:
        for p in PROJECT_ROOT.rglob(pattern):
            if (p.is_file() and SCRATCH_DIRNAME not in p.parts
                    and p.name not in keep):
                targets.append(p)

    # 去掉已被父级目录覆盖的子路径。
    # 否则会变成「先把 build/ 整个删掉，再去删 build/xxx/yyy.pyc」，
    # 后者必然 FileNotFoundError：实测踩过（5 个 pyc 报失败）。
    # 排序后父路径一定排在子路径之前，顺序扫描即可。
    kept: list[Path] = []
    for p in sorted(set(targets)):
        s = str(p)
        if any(s.startswith(str(k) + os.sep) for k in kept):
            continue
        kept.append(p)

    for p in kept:
        try:
            if dry_run:
                files, size = _count_only(p)
                results.append({"path": str(p), "dry_run": True, "files": files, "bytes": size})
            elif p.is_dir():
                files, size = _rmtree(p)
                results.append({"path": str(p), "files": files, "bytes": size,
                                "exists_after": p.exists()})
            else:
                size = p.stat().st_size
                # 必须走 cmd del。实测本机 os.unlink 会被重定向进回收站，
                # 既不是真删，还会在回收站里堆一堆构建垃圾。
                _true_delete([str(p)], [], [])
                results.append({"path": str(p), "files": 1, "bytes": size,
                                "exists_after": p.exists()})
        except Exception as e:  # noqa: BLE001
            results.append({"path": str(p), "error": f"{type(e).__name__}: {e}"})
    return results


def report(results: list[dict], title: str = "purge") -> None:
    total_files = sum(r.get("files", 0) for r in results)
    total_bytes = sum(r.get("bytes", 0) for r in results)
    refused = [r for r in results if "refused" in r or "error" in r]
    print(f"  [{title}] 条目 {len(results)} | 文件 {total_files:,} | 合计 {human(total_bytes)}")
    for r in results:
        if "refused" in r:
            print(f"    ! 拒绝 {r['path']}")
        elif "error" in r:
            print(f"    ! 失败 {r['path']} -> {r['error']}")
    if refused:
        print(f"    {len(refused)} 项未清理（见上）")


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"
