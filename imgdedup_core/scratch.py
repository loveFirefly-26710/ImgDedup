"""删除路径 B：真删，但被白名单死死圈在 `.scratch/` 里。

与 action.py 是两条物理隔离的路径：这里清理开发期的构建垃圾，走 `cmd /c del`
绕开回收站，所以 `_assert_purgeable` 会拒绝任何落在白名单之外的路径。

刻意不 import 本项目其它模块 —— 它是「清理工具」，不该依赖被清理的东西。"""

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

ALLOWED_ROOTS = (SCRATCH_DIRNAME,)

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
    pass



def long_path(p) -> str:
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



def scratch_root() -> Path:
    d = PROJECT_ROOT / SCRATCH_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def scratch_path(*parts) -> Path:
    p = scratch_root().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p



def _assert_purgeable(target) -> Path:
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



def _collect_tree(root: str) -> tuple[list[str], list[str], list[str], int]:
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
                links.append(full)
                continue
            if entry.is_dir(follow_symlinks=False):
                rec(full)
            else:
                try:
                    size += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
                files.append(full)
        dirs.append(d)

    rec(root)
    return files, dirs, links, size


def _cmd(args: list[str]) -> None:
    try:
        subprocess.run(["cmd", "/c"] + args, capture_output=True, timeout=300)
    except Exception:
        pass


def _true_delete(files: list[str], dirs: list[str], links: list[str]) -> None:
    for i in range(0, len(files), 40):
        _cmd(["del", "/f", "/q"] + files[i:i + 40])

    for link in links:
        try:
            os.rmdir(link)
        except OSError:
            try:
                os.unlink(link)
            except OSError:
                pass

    for d in dirs:
        _cmd(["rd", "/q", d])


def _rmtree(p: Path) -> tuple[int, int]:
    files, dirs, links, size = _collect_tree(str(p))
    _true_delete(files, dirs, links)
    return len(files), size


def sweep_recycle_bin(roots, *, since: float = 0.0) -> int:
    import struct

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
            except Exception:
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
    root = scratch_root()
    results = []
    for entry in sorted(root.iterdir()):
        try:
            results.append(purge(entry, dry_run=dry_run))
        except PurgeRefused as e:
            results.append({"path": str(entry), "refused": str(e)})
    return results


def purge_build_junk(*, dry_run: bool = False) -> list[dict]:
    results = []
    targets: list[Path] = []

    for name in BUILD_JUNK_DIRS:
        for p in PROJECT_ROOT.rglob(name):
            if p.is_dir() and SCRATCH_DIRNAME not in p.parts:
                targets.append(p)
    keep = {"imgdedup.spec"}
    for pattern in BUILD_JUNK_GLOBS:
        for p in PROJECT_ROOT.rglob(pattern):
            if (p.is_file() and SCRATCH_DIRNAME not in p.parts
                    and p.name not in keep):
                targets.append(p)

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
                _true_delete([str(p)], [], [])
                results.append({"path": str(p), "files": 1, "bytes": size,
                                "exists_after": p.exists()})
        except Exception as e:
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
