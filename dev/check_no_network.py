
"""把 socket / urllib 的出网口堵住再跑测试，抓「顺手联网」的测试。

仓库当前没有 tests/ 目录，所以它会 discover 到 0 项、直接打印 OK —— 是空跑。
补回测试套件后再接进 CI 即可生效（见 .github/workflows/ci.yml 头部说明）。
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

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def install_guards() -> None:
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
        return 1
    print("OK：没有任何测试联网 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
