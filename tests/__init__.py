"""测试包。

只做一件事：把仓库根目录放进 `sys.path`。这样不论用
`python -m unittest discover -s tests` 还是别的入口跑，`import imgdedup_core`
都能解析到 —— 不依赖调用者的工作目录。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 必须在任何 PySide6 导入之前设好：平台插件是在 Qt 首次初始化时选定的。
# 放这里而不是每个测试模块里，是为了不出现「为了副作用而 import」——
# 那种 import 会被静态检查判成未使用导入。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
