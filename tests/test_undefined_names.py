"""pyflakes 门禁：抓「用到了但没定义的名字」（F821）。

这类问题的运行时表现必然是 `NameError`，而且往往只在某条分支上才炸 ——
静态检查是唯一能在改动当时就拦住它的手段。

不追求「零告警」：仓库里有两处刻意的接线式导入会报 F401，它们在下面列着。
"""

import os
import re
import sys
import unittest

try:
    from pyflakes import api as fa
    from pyflakes import reporter as fr
except ImportError:                     # pragma: no cover
    fa = None
    fr = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("imgdedup_core", "imgdedup_ui", "dev", "tests")

# 出现即失败：这些必然导致 NameError / 语义错乱
FATAL = {
    "UndefinedName", "UndefinedLocal", "UndefinedExport",
    "ImportStarUsage", "FutureFeatureNotDefined", "TwoStarRedefinition",
    "RedefinedWhileUnused",
}

# 刻意保留的未使用导入：导入即代表「这个模块能加载」，是接线检查
ALLOWED_UNUSED_IMPORTS = {
    "imgdedup_core.action", "imgdedup_core.cache", "imgdedup_core.feature",
    "imgdedup_core.group", "imgdedup_core.scan", "imgdedup_ui.window.MainWindow",
}

# 刻意保留的未使用局部变量：必须持有引用，否则会被 GC 掉
ALLOWED_UNUSED_VARS = {("dev/probe_modes_e2e.py", "app")}


class _Collector(fr.Reporter if fr else object):

    def __init__(self):
        if fr:
            super().__init__(sys.stdout, sys.stderr)
        self.items = []

    def flake(self, message):
        self.items.append(message)

    def unexpectedError(self, filename, msg):      # pragma: no cover
        self.items.append(msg)

    def syntaxError(self, filename, msg, lineno, offset, text):  # pragma: no cover
        self.items.append(msg)


def _py_files() -> list[str]:
    out = []
    for base in TARGETS:
        for dp, dn, fn in os.walk(os.path.join(ROOT, base)):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in fn:
                if f.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return sorted(out)


def _collect() -> list[tuple[str, object]]:
    out = []
    for rel in _py_files():
        c = _Collector()
        fa.checkPath(os.path.join(ROOT, rel), c)
        for m in c.items:
            out.append((rel.replace(os.sep, "/"), m))
    return out


@unittest.skipUnless(fa is not None, "没装 pyflakes（pip install -r requirements-dev.txt）")
class TestUndefinedNames(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.messages = _collect()

    def test_no_undefined_names(self):
        bad = [f"{rel}:{m.lineno}: {m}"
               for rel, m in self.messages
               if type(m).__name__ in FATAL]
        self.assertEqual(bad, [], "有未定义的名字（运行时必然 NameError）：\n  "
                         + "\n  ".join(bad))

    def test_only_the_expected_unused_imports(self):
        pattern = re.compile(r"'([^']+)' imported but unused")
        bad = []
        for rel, m in self.messages:
            if type(m).__name__ != "UnusedImport":
                continue
            match = pattern.search(str(m))
            name = match.group(1) if match else str(m)
            if name not in ALLOWED_UNUSED_IMPORTS:
                bad.append(f"{rel}:{m.lineno}: {m}")
        self.assertEqual(bad, [], "出现了计划外的未使用导入：\n  " + "\n  ".join(bad))

    def test_only_the_expected_unused_variables(self):
        pattern = re.compile(r"local variable '([^']+)'")
        bad = []
        for rel, m in self.messages:
            if type(m).__name__ != "UnusedVariable":
                continue
            match = pattern.search(str(m))
            name = match.group(1) if match else ""
            if (rel, name) not in ALLOWED_UNUSED_VARS:
                bad.append(f"{rel}:{m.lineno}: {m}")
        self.assertEqual(bad, [], "出现了计划外的未使用变量：\n  " + "\n  ".join(bad))

    def test_every_file_is_actually_parsed(self):
        """判据自检：如果 pyflakes 一个文件都没读到，上面三条会「全绿」。"""
        self.assertGreater(len(_py_files()), 30)

    def test_gate_would_catch_a_real_undefined_name(self):
        """注入验证：现场造一个未定义名字，确认门禁真的会红。"""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="imgdedup_f821_") as tmp:
            path = os.path.join(tmp, "broken.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("def f():\n    return definitely_not_defined_xyz\n")
            c = _Collector()
            fa.checkPath(path, c)
        names = {type(m).__name__ for m in c.items}
        self.assertIn("UndefinedName", names)


if __name__ == "__main__":
    unittest.main()
