"""ImgDedup 核心引擎。

硬性约束：本包内不得出现任何 GUI 依赖（PySide6 / PyQt / tkinter）。
tests/test_guardrails.py 会断言这一点，它是"引擎可被独立测试与替换"的保证。
"""

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "infra",
    "scan",
    "decode",
    "feature",
    "group",
    "catalog",
    "scratch",
]
