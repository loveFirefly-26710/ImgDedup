"""从 PyPI 取 opencv-python 轮子，抽出 cv2 放进 extra/。

只认与本机 Python 版本、平台匹配的轮子；abi3 的兼容规则见 `wheel_matches`。"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

PYPI_PACKAGE = "opencv-python"

SKIP_DIR_NAMES = (
    "data",
    "__pycache__",
)
SKIP_NAME_PARTS = (
    "opencv_videoio_ffmpeg",
)

Progress = Optional[Callable[[int, int, str], None]]


def _log(msg: str, cb: Progress = None) -> None:
    if cb is not None:
        cb(0, 0, msg)


def should_skip(rel_parts: tuple) -> bool:
    for part in rel_parts:
        low = part.lower()
        if low in SKIP_DIR_NAMES:
            return True
        if any(k in low for k in SKIP_NAME_PARTS):
            return True
    return False


def platform_tag() -> str:
    m = platform.machine().lower()
    return "win_amd64" if m in ("amd64", "x86_64") else "win32"


def wheel_matches(filename: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    stem = filename[:-4]
    parts = stem.split("-")
    if len(parts) < 5:
        return False
    py_tags, abi_tags, plat_tag = parts[-3], parts[-2], parts[-1]
    if platform_tag() not in plat_tag.split("."):
        return False

    want_minor = sys.version_info.minor
    want_major = sys.version_info.major

    for abi in abi_tags.split("."):
        if abi == "abi3":
            for p in py_tags.split("."):
                if p.startswith("cp3") and p[3:].isdigit():
                    if want_major == 3 and int(p[3:]) <= want_minor:
                        return True
        elif abi == f"cp{want_major}{want_minor}":
            if f"cp{want_major}{want_minor}" in py_tags.split("."):
                return True
    return False


def find_wheel(timeout: int = 60, cb: Progress = None) -> dict:
    url = f"https://pypi.org/pypi/{PYPI_PACKAGE}/json"
    _log(f"查询 PyPI：{url}", cb)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        meta = json.load(r)

    def ver_key(v: str):
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    tried = 0
    for ver in sorted(meta["releases"], key=ver_key, reverse=True):
        tried += 1
        for f in meta["releases"][ver]:
            if wheel_matches(f["filename"]):
                _log(f"找到适配的版本：{ver}", cb)
                return f
        if tried >= 40:
            break
    raise RuntimeError(
        f"在 PyPI 上前 40 个版本里都没找到适配 "
        f"{platform_tag()} / Python {sys.version_info.major}."
        f"{sys.version_info.minor} 的 {PYPI_PACKAGE} 轮子。\n"
        f"可以手动下载后放进 extra/cv2/。")


def download(url: str, dest: Path, timeout: int = 120,
             cb: Progress = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if cb is not None:
                    cb(got, total, f"已下载 {got / 1048576:.1f} / "
                                   f"{total / 1048576:.1f} MB")
        tmp.replace(dest)
    return dest


def extract_cv2(wheel: Path, extra_dir: Path, cb: Progress = None) -> tuple[int, int]:
    dst = extra_dir / "cv2"
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    total = 0
    with zipfile.ZipFile(wheel) as z:
        members = [m for m in z.namelist() if m.startswith("cv2/")]
        if not members:
            raise RuntimeError(
                f"{wheel.name} 里没有 cv2/ 目录 —— 可能不是 opencv 的包")
        for m in members:
            rel = m[len("cv2/"):]
            if not rel or m.endswith("/"):
                continue
            rel_parts = tuple(rel.split("/"))
            if should_skip(rel_parts):
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, "wb") as out:
                data = src.read()
                out.write(data)
            n += 1
            total += len(data)
            if cb is not None and n % 10 == 0:
                cb(0, 0, f"已解出 {n} 个文件…")
    if not (dst / "cv2.pyd").is_file():
        raise RuntimeError(
            f"解出来没有 cv2.pyd（{dst}）—— 这个轮子可能不是 Windows 版")
    return n, total


def install(extra_dir: Path, cb: Progress = None) -> tuple[int, int]:
    info = find_wheel(cb=cb)
    size_mb = info.get("size", 0) / 1048576
    _log(f"下载 {info['filename']}（{size_mb:.1f} MB）", cb)
    with tempfile.TemporaryDirectory() as td:
        whl = download(info["url"], Path(td) / info["filename"], cb=cb)
        _log("正在解压…", cb)
        return extract_cv2(whl, extra_dir, cb=cb)
