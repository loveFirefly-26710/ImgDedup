"""cv2 的定位与加载 —— 全仓唯一真正 `import cv2` 的地方。

cv2 是外置组件（86 MB，放 `<exe 同级>/extra/cv2/`），不随包分发。其它模块一律
通过 `lazy_cv2` 代理访问：导入时不碰 cv2，第一次取属性才去加载。这样「没装 cv2」
只影响真正用到它的功能，不会拖垮首屏。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

EXTRA_DIRNAME = "extra"

CV2_DIRNAME = "cv2"

_loaded = None
_failed_reason: Optional[str] = None


def candidate_roots() -> list[Path]:
    out: list[Path] = []

    def add(p: Optional[Path]) -> None:
        if p is not None and p not in out:
            out.append(p)

    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:
        exe_dir = None
    if exe_dir is not None:
        add(exe_dir / EXTRA_DIRNAME)
        add(exe_dir / "_internal" / EXTRA_DIRNAME)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        add(Path(meipass) / EXTRA_DIRNAME)
    here = Path(__file__).resolve().parent
    add(here.parent / EXTRA_DIRNAME)
    return out


def extra_dir() -> Path:
    try:
        return Path(sys.executable).resolve().parent / EXTRA_DIRNAME
    except Exception:
        return Path(__file__).resolve().parent.parent / EXTRA_DIRNAME


def cv2_dir() -> Path:
    return extra_dir() / CV2_DIRNAME


def install_path_hook() -> None:
    for p in reversed(candidate_roots()):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def require():
    global _loaded, _failed_reason
    if _loaded is not None:
        return _loaded
    if _failed_reason is not None:
        raise ImportError(_failed_reason)

    try:
        import cv2
        _loaded = cv2
        return cv2
    except ImportError:
        pass

    install_path_hook()
    try:
        import cv2
        _loaded = cv2
        return cv2
    except ImportError as e:
        _failed_reason = missing_hint() + f"\n\n（底层报错：{e}）"
        raise ImportError(_failed_reason) from e


def is_available() -> bool:
    try:
        require()
        return True
    except ImportError:
        return False


def refresh() -> bool:
    global _loaded, _failed_reason
    _loaded = None
    _failed_reason = None
    return is_available()


def version() -> Optional[str]:
    try:
        return str(getattr(require(), "__version__", "")) or None
    except ImportError:
        return None


def found_at() -> Optional[str]:
    try:
        mod = require()
    except ImportError:
        return None
    f = getattr(mod, "__file__", None)
    return str(Path(f).parent) if f else None


def missing_hint() -> str:
    target = cv2_dir()
    roots = candidate_roots()
    extra = extra_dir()
    return (
        "缺少图像处理组件 cv2（它占 86 MB，已改为单独下载）。\n"
        "\n"
        f"请把 cv2 文件夹放到这里：\n"
        f"    {target}\n"
        "\n"
        f"也就是在 {extra} 下面有一个 cv2 目录，\n"
        "里面应当能看到 cv2.pyd 这个文件。\n"
        "\n"
        "获取方式（任选其一）：\n"
        "  1. 启动程序时点「自动下载」，它会自己从 PyPI 下好并放到上面那个位置；\n"
        "  2. 或手动下载 opencv-python 的 Windows 版 whl，\n"
        "     解压后把里面的 cv2 文件夹整个拷到上面那个位置。\n"
        f"\n（本程序也会在这些位置找：{[str(p) for p in roots]}）"
    )


class _LazyCv2:

    def __getattr__(self, name: str):
        return getattr(require(), name)

    def __repr__(self) -> str:
        return "<lazy cv2 proxy>"


lazy_cv2 = _LazyCv2()
