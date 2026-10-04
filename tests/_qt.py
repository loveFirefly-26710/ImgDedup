"""Qt 测试的公共脚手架：强制离屏平台、单例 QApplication、事件循环冲刷。

必须在 `import PySide6` 之前设 `QT_QPA_PLATFORM`：平台插件是在 Qt 首次
初始化时选定的，之后再设没用。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_app = None


def app():
    """拿到（必要时创建）唯一的 QApplication。

    整个测试进程只能有一个 QApplication，反复创建会让 Qt 直接 abort。
    """
    global _app
    from PySide6.QtWidgets import QApplication

    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


def flush() -> None:
    """把待处理的事件跑一遍（控件更新、布局、延时任务都在队列里）。"""
    from PySide6.QtCore import QCoreApplication

    a = QCoreApplication.instance()
    if a is not None:
        a.processEvents()
