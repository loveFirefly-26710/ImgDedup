"""堵住出网口再跑全量测试：确保测试永不依赖网络。

## 为什么需要

CI 上跑测试时，任何"顺手下载模型 / cv2"的代码路径都会让构建又慢又脆
（网络抖一下就红）。实测踩过一次：缺模型时整个测试套件卡在网络上
十几分钟，根因是 `ClipEncoder.load()` 内部会静默调 `ensure_model()`
去下 85 MB 的模型。

修掉之后需要一条门禁防止它悄悄回来，就是本脚本。

## 做法

把 `socket.create_connection` 与 `urllib.request.urlopen` 换成会抛异常的
桩（放行本机回环与 `file://`），然后跑全量测试：

- 任何尝试联网的地方立刻失败并报出测试名，而不是静默卡几分钟；
- 退出码非 0 = 有测试在联网（或测试本身红了，详情看上面的 unittest 输出）。

## 为什么不用"临时把 resources/models 改名藏起来"

那需要移动用户的文件，一旦进程被强杀（SIGTERM 不走 `finally`），
目录就留在改名状态：这个坑真踩过两次。本脚本不动任何文件。

用法：
    python dev/check_no_network.py
"""

from __future__ import annotations

import os
import socket
import sys
import unittest
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 放行的地址：本机回环（Qt / 子进程测试可能用到）
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def install_guards() -> None:
    """把出网口换成会抛异常的桩。"""
    real_create = socket.create_connection

    def guard_create(address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in LOCAL_HOSTS:
            return real_create(address, *a, **kw)
        raise RuntimeError(f"[禁网] 测试尝试联网：{address}")

    socket.create_connection = guard_create

    import urllib.request

    real_urlopen = urllib.request.urlopen

    def guard_urlopen(url, *a, **kw):
        # `file://` 不算联网：用本地文件冒充"下载"是测试的合法手段
        # （`test_download_file_reports_progress` 就是这么验进度回调的）。
        target = getattr(url, "full_url", url)
        if str(target).lower().startswith("file:"):
            return real_urlopen(url, *a, **kw)
        raise RuntimeError(f"[禁网] 测试尝试联网：{target}")

    urllib.request.urlopen = guard_urlopen


def main() -> int:
    install_guards()
    os.chdir(str(PROJ))

    suite = unittest.TestLoader().discover("tests", pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    offenders = [name for name, tb in (result.failures + result.errors)
                 if "尝试联网" in str(tb)]
    print()
    if offenders:
        print("!! 这些测试在联网（CI 上会又慢又脆）：")
        for name in offenders:
            print("   -", name)
        return 1
    if not result.wasSuccessful():
        # 测试红了，但原因不是联网（unittest 已经把详情打在上面）。
        # 这里不能说 OK，否则看日志的人会以为这一趟全绿。
        return 1
    print("OK：没有任何测试联网 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
