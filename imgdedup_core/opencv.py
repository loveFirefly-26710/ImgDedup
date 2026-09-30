"""外置 `cv2` 的定位与惰性加载。

## 为什么要把 cv2 挪出安装包

`cv2.pyd` 一个文件就 85.7 MiB，占整个程序体积的 21.7%。
但它只在"真正开始扫描"时才用得上，所以做成可选组件：
不随包分发，用户按需下载后放到指定目录。

## 放哪儿（这是唯一的约定，别在别处硬编码）

    <ImgDedup.exe 所在目录>/extra/cv2/

也就是和 exe 同级的一个 `extra` 文件夹，里面放 `cv2` 包目录：

    ImgDedup.exe
    _internal/                 ← 程序本体，别动
    extra/                     ← 外置组件（用户自己放）
      cv2/
        __init__.py
        cv2.pyd
        config.py

之所以选"和 exe 同级"而不是塞进 `_internal/`：
`_internal/` 是打包器的私有目录，用户不该往里翻；
放在外面看得见、好替换、升级程序时不会被覆盖。
（免安装版和安装版都是同一套相对位置，安装版里就是
`%LOCALAPPDATA%\\Programs\\ImgDedup\\extra\\cv2\\`。）

## 为什么必须有这个模块（不能直接 `import cv2`）

`embed.py` / `feature.py` 原来在模块顶层就 `import cv2`。
一旦包里没有 cv2，导入它们立刻 `ImportError` ——
整个程序连窗口都建不出来，用户只会看到一个没有任何提示的闪退，
根本不知道"少了个组件"。

所以：顶层不导入，改成惰性代理（`lazy_cv2`），
第一次真正用到 `cv2.xxx` 时才去定位并导入。
这样既能"缺组件时给出人话提示"，又不用改一堆调用点。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

#: 外置组件目录名（相对 exe 所在目录）。约定，改动要同步改文档。
EXTRA_DIRNAME = "extra"

#: 组件自己的目录名
CV2_DIRNAME = "cv2"

#: 从 PyPI 取哪个发行版。`opencv-python` 是官方包；
#: 不用 `-contrib`（本项目没用到 contrib 模块，包还更大）。
PYPI_PACKAGE = "opencv-python"

_loaded = None          # 缓存已导入的 cv2 模块
_failed_reason: Optional[str] = None


def candidate_roots() -> list[Path]:
    """按优先级列出可能放着 `cv2` 的目录（要加进 `sys.path` 的那些）。

    顺序和 `imgdedup_core.embed.bundled_root()` 同一套思路：
    越接近运行形态越优先。
    """
    out: list[Path] = []

    def add(p: Optional[Path]) -> None:
        if p is not None and p not in out:
            out.append(p)

    # 1) exe 同级的 extra/：免安装版、安装版都走这条
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:  # noqa: BLE001
        exe_dir = None
    if exe_dir is not None:
        add(exe_dir / EXTRA_DIRNAME)
        # 2) onedir 的 _internal/extra：给"手动塞进包内"留一条后路
        add(exe_dir / "_internal" / EXTRA_DIRNAME)
    # 3) onefile 的解包目录
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        add(Path(meipass) / EXTRA_DIRNAME)
    # 4) 源码树的 extra/：开发态
    here = Path(__file__).resolve().parent
    add(here.parent / EXTRA_DIRNAME)
    return out


def extra_dir() -> Path:
    """推荐给用户的摆放位置（用于提示文案 / 下载落地）。

    取"exe 同级"那一项；拿不到 exe 路径时退回源码树的 `extra/`。
    """
    try:
        return Path(sys.executable).resolve().parent / EXTRA_DIRNAME
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / EXTRA_DIRNAME


def cv2_dir() -> Path:
    """`cv2` 包应该待的完整目录（`<extra>/cv2`）。"""
    return extra_dir() / CV2_DIRNAME


def install_path_hook() -> None:
    """把候选目录插到 `sys.path` 前面。

    必须插在前面：site-packages 里可能装着一个旧版 cv2，
    用户的 `extra/` 应该优先。
    """
    for p in reversed(candidate_roots()):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def require():
    """导入并返回 cv2 模块；失败时抛带操作说明的 ImportError。

    调用方（`lazy_cv2` 代理）负责把异常原样抛给上层 ——
    宁可抛错也不要静默降级：静默降级会让人以为"去重结果不对"是算法问题，
    实际是少了个组件。
    """
    global _loaded, _failed_reason
    if _loaded is not None:
        return _loaded
    if _failed_reason is not None:
        raise ImportError(_failed_reason)

    # 1) 先直接试：开发态、或用户把 cv2 装进了 Python 环境
    try:
        import cv2  # noqa: PLC0415
        _loaded = cv2
        return cv2
    except ImportError:
        pass

    # 2) 再把外置目录挂上去试
    install_path_hook()
    try:
        import cv2  # noqa: PLC0415
        _loaded = cv2
        return cv2
    except ImportError as e:  # noqa: BLE001
        _failed_reason = missing_hint() + f"\n\n（底层报错：{e}）"
        raise ImportError(_failed_reason) from e


def is_available() -> bool:
    """cv2 能不能用（会真的尝试导入一次，结果被缓存）。"""
    try:
        require()
        return True
    except ImportError:
        return False


def refresh() -> bool:
    """清掉缓存重新探测。装完组件之后必须调它。

    为什么需要：`require()` 把失败原因缓存在 `_failed_reason` 里
    （否则每次属性访问都要重试一次导入）。于是"用户刚把 cv2 装好"
    这个场景下，缓存里还记着"没有"：程序会继续报缺组件，
    用户会以为"下载了也没用"。
    """
    global _loaded, _failed_reason
    _loaded = None
    _failed_reason = None
    return is_available()


def version() -> Optional[str]:
    """能拿到就返回版本号，否则 None。"""
    try:
        return str(getattr(require(), "__version__", "")) or None
    except ImportError:
        return None


def found_at() -> Optional[str]:
    """cv2 实际是从哪个目录加载的（诊断用）。"""
    try:
        mod = require()
    except ImportError:
        return None
    f = getattr(mod, "__file__", None)
    return str(Path(f).parent) if f else None


def missing_hint() -> str:
    """给用户看的"怎么补上"说明。要能照着做，所以写绝对路径。"""
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
    """惰性 cv2 代理。

    让 `embed.py` / `feature.py` 可以照旧写 `cv2.resize(...)`，
    既不在导入期把程序炸掉，也不用改任何调用点 ——
    真正的导入推迟到第一次属性访问。

    代价是每次属性访问多一层 `__getattr__`，可忽略（不是热路径）。
    """

    def __getattr__(self, name: str):
        return getattr(require(), name)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<lazy cv2 proxy>"


#: 给业务模块用的替身：`from .opencv import lazy_cv2 as cv2`
lazy_cv2 = _LazyCv2()
