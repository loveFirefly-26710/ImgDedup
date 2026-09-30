"""静态门禁：不许出现"用到了但没定义"的名字（pyflakes F821）。

## 为什么需要

`imgdedup_ui/window.py` 曾经漏了一行导入：

    d = ask_delete(self, len(paths), ...)      # ← ask_delete 从没导入过

后果是所有删除入口全部 `NameError`：右键「立即删除」、
「删除本组其余全部」、「删除选中的这一张…」、「执行删除…」四个调用点。
`dialogs.py` 写得好好的、签名也对得上，就是没被导入。
`py_compile` / `ast.parse` 都不报，测试也不报（没有测试覆盖那条路径），
只有用户点下去才炸。

## 为什么用 pyflakes 而不是自己写

一开始我写了个按作用域扫名字的脚本，结果全是误报：它没处理
闭包（嵌套函数引用外层的 `log` / `D` / `task`）和类体作用域
（`def rowCount(self, parent=QModelIndex())` 的默认值在类体里求值）。
把那些规则补全就等于重写一个 linter。

pyflakes 是纯 Python、无依赖、装起来 100 KB，精确处理这些规则。
别自己写 linter。

## 门禁范围

只把 F821（undefined name） 设为硬失败：它对应"运行时必炸"。
「导入但未使用」这类不进硬门禁（有意的"导入即测试"会被误判），
由清理流程人工处理。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

PROJ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJ))

TARGETS = ["imgdedup_core", "imgdedup_ui", "dev", "tests"]

#: 未定义名的报错形态（pyflakes 的 F821）
UNDEFINED = re.compile(r"undefined name", re.I)


def run_pyflakes() -> tuple[int, str]:
    files: list[str] = []
    for d in TARGETS:
        files.extend(str(p) for p in sorted((PROJ / d).glob("*.py")))
    r = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                       cwd=str(PROJ), capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class TestNoUndefinedNames(unittest.TestCase):
    def test_pyflakes_available(self):
        """门禁依赖 pyflakes（在 `requirements-dev.txt` 里）。

        不用 `skipTest` 悄悄放过：静默跳过的门禁不是门禁。
        """
        r = subprocess.run([sys.executable, "-m", "pyflakes", "--version"],
                           capture_output=True, text=True)
        self.assertEqual(
            r.returncode, 0,
            "没装 pyflakes，静态门禁跑不了。先： pip install -r requirements-dev.txt")

    def test_no_undefined_names(self):
        code, out = run_pyflakes()
        bad = [ln for ln in out.splitlines() if UNDEFINED.search(ln)]
        self.assertEqual(
            bad, [],
            "发现未定义的名字（运行时必然 NameError）：\n  " + "\n  ".join(bad))

    def test_ask_delete_is_reachable_from_window(self):
        """直接钉住那个具体事故：`window` 里必须能解析到 `ask_delete`。

        比源码文本断言更结实：它检查的是模块命名空间里真的有这个名字。
        """
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from imgdedup_ui import window as W

        self.assertTrue(hasattr(W, "ask_delete"),
                        "window 里没有 ask_delete —— 所有删除入口都会 NameError")


if __name__ == "__main__":
    unittest.main()
