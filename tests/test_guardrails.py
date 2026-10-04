"""分层守卫：这几条是架构约束，违反了不会报错，只会悄悄退化。

- 核心引擎不许拉 GUI（否则「引擎能独立测试、能整体替换」就没了）
- 界面层对核心引擎的引用只能写在方法里（否则首屏被 numpy/cv2 的导入拖死）
- 只有 `opencv.py` 能真正 `import cv2`（否则缺外置组件就闪退）
- `scratch.py` 不许引用本项目任何模块（清理工具不该依赖被清理的东西）
"""

import ast
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE_MODULES = [
    "imgdedup_core.infra", "imgdedup_core.scan", "imgdedup_core.decode",
    "imgdedup_core.feature", "imgdedup_core.group", "imgdedup_core.cache",
    "imgdedup_core.catalog", "imgdedup_core.action", "imgdedup_core.scratch",
    "imgdedup_core.opencv", "imgdedup_core.cv2_fetch", "imgdedup_core.embed",
]

UI_MODULES = [
    "imgdedup_ui.theme", "imgdedup_ui.widgets", "imgdedup_ui.model",
    "imgdedup_ui.report", "imgdedup_ui.dialogs", "imgdedup_ui.worker",
    "imgdedup_ui.window", "imgdedup_ui._entry_symbols", "imgdedup_ui.__main__",
]


def _source(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _tree(rel: str) -> ast.Module:
    return ast.parse(_source(rel), filename=rel)


def _top_level_imports(tree: ast.Module) -> list[str]:
    """只看模块顶层（含 `if TYPE_CHECKING` 之外的第一层），不含函数体内部。"""
    out = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
    return out


class TestCoreDoesNotPullGui(unittest.TestCase):

    def test_importing_every_core_module_leaves_pyside_out(self):
        code = (
            "import importlib, sys\n"
            f"mods = {CORE_MODULES!r}\n"
            "for m in mods:\n"
            "    importlib.import_module(m)\n"
            "bad = sorted(n for n in sys.modules if n.split('.')[0] in "
            "('PySide6', 'PyQt5', 'PyQt6', 'tkinter'))\n"
            "print('LEAKED:' + ','.join(bad))\n"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("LEAKED:", r.stdout)
        leaked = r.stdout.split("LEAKED:", 1)[1].strip()
        self.assertEqual(leaked, "", f"核心引擎拉起了 GUI 依赖：{leaked}")

    def test_ui_init_does_not_import_core(self):
        names = _top_level_imports(_tree("imgdedup_ui/__init__.py"))
        self.assertEqual([n for n in names if n.startswith("imgdedup_core")], [])


class TestLazyCoreImports(unittest.TestCase):

    def test_ui_init_and_worker_do_not_import_core_at_module_level(self):
        for rel in ("imgdedup_ui/__init__.py", "imgdedup_ui/worker.py"):
            with self.subTest(rel=rel):
                names = _top_level_imports(_tree(rel))
                bad = [n for n in names if n.startswith("imgdedup_core")]
                self.assertEqual(bad, [], f"{rel} 在顶层导入了核心引擎：{bad}")

    def test_worker_defers_core_imports_into_methods(self):
        tree = _tree("imgdedup_ui/worker.py")
        inside = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inside += [n for n in _top_level_imports(ast.Module(
                    body=node.body, type_ignores=[])) if n.startswith("imgdedup_core")]
        self.assertTrue(inside, "worker.py 里应能看到方法内部的延迟导入")


class TestOnlyOpencvImportsCv2(unittest.TestCase):

    def test_no_top_level_cv2_import_anywhere_else(self):
        offenders = []
        for rel in _all_py():
            if rel.replace(os.sep, "/").endswith("imgdedup_core/opencv.py"):
                continue
            names = _top_level_imports(_tree(rel))
            if any(n == "cv2" or n.startswith("cv2.") for n in names):
                offenders.append(rel)
        self.assertEqual(offenders, [],
                         f"这些模块在顶层直接 import cv2：{offenders}")

    def test_opencv_is_the_one_that_does(self):
        src = _source("imgdedup_core/opencv.py")
        self.assertIn("import cv2", src)


class TestScratchIsStandalone(unittest.TestCase):

    def test_scratch_imports_nothing_from_this_project(self):
        tree = _tree("imgdedup_core/scratch.py")
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    bad.append(f"相对导入 {'.' * node.level}{node.module}")
                elif (node.module or "").startswith("imgdedup"):
                    bad.append(node.module)
            elif isinstance(node, ast.Import):
                bad += [a.name for a in node.names if a.name.startswith("imgdedup")]
        self.assertEqual(bad, [], f"scratch.py 引用了本项目模块：{bad}")

    def test_scratch_never_mentions_action_in_code(self):
        """把注释与字符串剥掉再找 `action` —— 注释里出现这个词不算违规。"""
        tree = _tree("imgdedup_core/scratch.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertNotIn("action", [a.name for a in node.names])
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "action")


def _all_py() -> list[str]:
    out = []
    for base in ("imgdedup_core", "imgdedup_ui", "dev"):
        for dp, dn, fn in os.walk(os.path.join(ROOT, base)):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in fn:
                if f.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return sorted(out)


class TestModuleCounts(unittest.TestCase):

    def test_core_module_list_matches_the_disk(self):
        found = {os.path.splitext(f)[0]
                 for f in os.listdir(os.path.join(ROOT, "imgdedup_core"))
                 if f.endswith(".py") and f != "__init__.py"}
        declared = {m.split(".")[-1] for m in CORE_MODULES}
        self.assertEqual(found, declared)

    def test_ui_module_list_matches_the_disk(self):
        found = {os.path.splitext(f)[0]
                 for f in os.listdir(os.path.join(ROOT, "imgdedup_ui"))
                 if f.endswith(".py") and f != "__init__.py"}
        declared = {m.split(".")[-1] for m in UI_MODULES}
        self.assertEqual(found, declared)


if __name__ == "__main__":
    unittest.main()
