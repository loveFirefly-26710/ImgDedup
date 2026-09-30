"""下载并安装外置组件 cv2（从 PyPI 取官方 wheel）。

供两处复用：
  · `dev/fetch_cv2.py`（打包的人用，命令行）；
  · 程序启动时那个"缺少 cv2"对话框（最终用户用，点一下就自动下好）。

## 选 wheel 的坑（这里踩过，别再犯）

不能用 `cp313` 去匹配文件名。 opencv 的 wheel 用的是
稳定 ABI（abi3），文件名长这样：

    opencv_python-4.11.0.86-cp37-abi3-win_amd64.whl

`cp37-abi3` 的意思是"CPython 3.7 及以上都能用" ——
所以文件名里根本不会出现 `cp313`。
按 `cp313` 过滤会得到"零个可用版本"，而 PyPI 上其实有几十个，
报错还会误导成"这个 Python 版本还没出包"。

另外不能只看最新版：`opencv-python` 5.x 的某些版本可能没出
对应平台的轮子，必须从新到旧逐个版本找。
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

#: 从 PyPI 取哪个发行版。`opencv-python` 是官方包；
#: 不用 `-contrib`（本项目没用到 contrib 模块，包还更大）。
PYPI_PACKAGE = "opencv-python"

#: 安装时要跳过的东西。每一条都写明理由，避免以后有人"顺手全拷"。
#: 和打包时的裁剪保持一致，否则"独立出来反而更大"。
SKIP_DIR_NAMES = (
    "data",          # 人脸检测的 haarcascade XML（6+ MiB），本工具不做人脸检测
    "__pycache__",
)
SKIP_NAME_PARTS = (
    "opencv_videoio_ffmpeg",   # 视频解码器（两个 DLL 共 51 MiB），只处理静态图
)

Progress = Optional[Callable[[int, int, str], None]]


def _log(msg: str, cb: Progress = None) -> None:
    if cb is not None:
        cb(0, 0, msg)


def should_skip(rel_parts: tuple) -> bool:
    """这个相对路径该不该跳过（打包与外置共用同一套规则）。"""
    for part in rel_parts:
        low = part.lower()
        if low in SKIP_DIR_NAMES:
            return True
        if any(k in low for k in SKIP_NAME_PARTS):
            return True
    return False


def platform_tag() -> str:
    """本机的 wheel 平台标签。"""
    m = platform.machine().lower()
    return "win_amd64" if m in ("amd64", "x86_64") else "win32"


def wheel_matches(filename: str) -> bool:
    """wheel 文件名适配"本解释器 + 本平台"吗？

    支持两种：
      · 稳定 ABI：`cp3X-abi3-<平台>`（opencv 就是这种），
        要求 `X <= 本机次版本号`；
      · 精确版本：`cp{主}{次}-cp{主}{次}-<平台>`。

    别只匹配 `cp313`：见模块开头的说明。
    """
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
            # 稳定 ABI：cp3X 且 X <= 本机次版本
            for p in py_tags.split("."):
                if p.startswith("cp3") and p[3:].isdigit():
                    if want_major == 3 and int(p[3:]) <= want_minor:
                        return True
        elif abi == f"cp{want_major}{want_minor}":
            if f"cp{want_major}{want_minor}" in py_tags.split("."):
                return True
    return False


def find_wheel(timeout: int = 60, cb: Progress = None) -> dict:
    """从新到旧找第一个能用的 wheel。返回 PyPI 的 file 字典。

    为什么要逐个版本找：最新版可能没出对应平台的轮子。
    只看最新版会误报"这个 Python 版本没有包"。
    """
    url = f"https://pypi.org/pypi/{PYPI_PACKAGE}/json"
    _log(f"查询 PyPI：{url}", cb)
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
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
        if tried >= 40:            # 够多了，别把整本历史翻完
            break
    raise RuntimeError(
        f"在 PyPI 上前 40 个版本里都没找到适配 "
        f"{platform_tag()} / Python {sys.version_info.major}."
        f"{sys.version_info.minor} 的 {PYPI_PACKAGE} 轮子。\n"
        f"可以手动下载后放进 extra/cv2/。")


def download(url: str, dest: Path, timeout: int = 120,
             cb: Progress = None) -> Path:
    """带进度地下载（进度回调给的是 已下载/总大小）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
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
    """把 wheel 里的 `cv2/` 解到 `extra_dir/cv2/`（跳过 SKIP 规则）。

    返回 (文件数, 总字节)。
    """
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
    """一条龙：找轮子 → 下载 → 解到 `extra_dir/cv2/`。返回 (文件数, 字节数)。"""
    info = find_wheel(cb=cb)
    size_mb = info.get("size", 0) / 1048576
    _log(f"下载 {info['filename']}（{size_mb:.1f} MB）", cb)
    with tempfile.TemporaryDirectory() as td:
        whl = download(info["url"], Path(td) / info["filename"], cb=cb)
        _log("正在解压…", cb)
        return extract_cv2(whl, extra_dir, cb=cb)
