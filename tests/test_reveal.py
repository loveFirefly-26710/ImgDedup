"""右键「在文件夹中显示」必须打开图片所在目录。

这是用户直接反馈的 bug：右键选「在文件夹中显示」会错误地打开「文档」。

根因不在路径拼错，而在怎么把实参交给 explorer.exe：

    # 错误写法：subprocess 会给含空格的实参加引号，explorer 不认
    subprocess.Popen(["explorer", f"/select,{p}"])

`subprocess.list2cmdline` 按 MSVCRT 规则转义 → 命令行变成
`explorer "/select,C:\\...\\流萤表情包 - 副本\\a.jpg"`。
`explorer.exe` 不按这套规则解析：它把开头那个引号当路径的一部分，
判定参数非法 → 忽略掉，打开默认的「文档」目录。

正确写法：自己拼一整条命令行字符串 `explorer /select,"<path>"`，`shell=False`。

这批测试钉住的是实参形式，不是"能不能弹出窗口"—— 后者在 CI/离屏下
没法断言，所以把"传参姿势"变成可断言的东西。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 会**真的弹出资源管理器窗口**的那条测试默认不跑。
#:
#: 理由：触发条件是 `~/Desktop/流萤表情包 - 副本` 存在（开发机上有），
#: 所以每跑一遍全量测试就弹一个窗口 —— 一天跑几十遍，看起来就像弹窗病毒。
#: 要跑它（改了 `_reveal` 想验真实行为时）：
#:     set IMGDEDUP_REVEAL_TESTS=1
#: 与 `IMGDEDUP_NET_TESTS` 是同一套约定。
REVEAL_TESTS = os.environ.get("IMGDEDUP_REVEAL_TESTS") == "1"

WINDIR = "C:" + os.sep + "Users" + os.sep + "me" + os.sep + "Desktop"


class _PopenSpy:
    """替换 `subprocess.Popen`，把调用实参记下来（不真的起进程）。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []

    def __enter__(self):
        import subprocess
        self._real = subprocess.Popen
        subprocess.Popen = self._fake           # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        import subprocess
        subprocess.Popen = self._real           # type: ignore[assignment]
        return False

    def _fake(self, *args, **kwargs):
        self.calls.append(args)
        self.kwargs.append(kwargs)
        return type("_Fake", (), {"returncode": 0})()

    @property
    def cmdline(self) -> str:
        """取第一条调用的命令行字符串（第一个位置参数）。"""
        return self.calls[0][0] if self.calls else ""


class TestRevealCommandShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _reveal_with(self, path: str):
        """调用 `_reveal`，返回 (spy)。"""

        from imgdedup_ui.window import MainWindow

        spy = _PopenSpy()
        w = MainWindow()
        try:
            with spy:
                w._reveal(path)
        finally:
            w.close()
        return spy

    def test_path_with_spaces_uses_string_not_list(self):
        """含空格的路径必须走字符串命令行，不能走 list。

        这是 bug 的核心：list 形式会被 subprocess 加引号 → explorer 打开「文档」。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "表情包 - 副本", "a.jpg")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").close()

            spy = self._reveal_with(p)
            self.assertTrue(spy.calls, "根本没有调用 Popen")
            first = spy.calls[0][0]
            self.assertIsInstance(
                first, str,
                "必须传字符串命令行（现在传的是 "
                f"{type(first).__name__}）—— list 会被 subprocess 加引号，"
                "explorer 会退回「文档」")
            self.assertFalse(spy.kwargs[0].get("shell"),
                             "shell=True 会把引号再转义一层，重新踩回那个坑")

    def test_command_contains_select_and_quoted_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "表情包 - 副本", "a b.jpg")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").close()

            spy = self._reveal_with(p)
            line = spy.cmdline
            self.assertIn("/select,", line,
                          "没有 /select, 就只是打开目录、不选中文件")
            self.assertIn('"', line, "含空格路径必须带引号，否则 explorer 会截断路径")
            # 引号必须紧跟在 /select, 之后包住整条路径
            self.assertIn('/select,"' + p + '"', line,
                          f"引号位置不对：{line}")
            # 且不能出现 subprocess 那套转义引号
            self.assertNotIn('\\"', line,
                             "出现了反斜杠转义引号 —— 说明又走回 list/shell 那条路了")

    def test_command_starts_with_explorer(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.jpg")
            open(p, "wb").close()
            spy = self._reveal_with(p)
            self.assertTrue(spy.cmdline.lower().startswith("explorer"),
                            f"命令必须以 explorer 开头：{spy.cmdline!r}")

    def test_missing_file_does_not_open_anything(self):
        """文件已不存在时不要打开窗口，并给出状态提示。"""
        from imgdedup_ui.window import MainWindow

        spy = _PopenSpy()
        w = MainWindow()
        try:
            with spy:
                w._reveal(os.path.join(WINDIR, "definitely_not_here_12345.jpg"))
            self.assertEqual(spy.calls, [], "文件不存在时不该打开资源管理器")
            self.assertIn("不存在", w.lbl_status.text())
        finally:
            w.close()

    def test_empty_path_is_safe(self):
        """空路径不该崩、也不该起进程。"""
        from imgdedup_ui.window import MainWindow

        spy = _PopenSpy()
        w = MainWindow()
        try:
            with spy:
                w._reveal("")
            self.assertEqual(spy.calls, [])
        finally:
            w.close()


class TestRevealRealWindowsBehaviour(unittest.TestCase):
    """本机实测：字符串命令行确实能打开目标目录。

    只在本机有那个中文目录时才跑，避免在别人机器上假失败。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _explorer_titles(self) -> set[str]:
        import ctypes

        user32 = ctypes.windll.user32
        titles: set[str] = set()

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if "文件资源管理器" in buf.value:
                    titles.add(buf.value)
            return True

        user32.EnumWindows(cb, 0)
        return titles

    def test_real_directory_opens(self):
        """真开一次资源管理器，用窗口标题确认开对了目录。

        **默认不跑**：它会真弹窗口，而全量测试一天可能跑几十遍 ——
        用户看到的就是"一直弹窗"。要跑就 `set IMGDEDUP_REVEAL_TESTS=1`。
        """
        if not REVEAL_TESTS:
            self.skipTest("会真弹资源管理器窗口，默认不跑"
                          "（要跑：set IMGDEDUP_REVEAL_TESTS=1）")
        target = os.path.join(os.path.expanduser("~"), "Desktop",
                              "流萤表情包 - 副本")
        if not os.path.isdir(target):
            self.skipTest(f"本机没有测试目录：{target}")
        files = [f for f in os.listdir(target)
                 if os.path.isfile(os.path.join(target, f))]
        if not files:
            self.skipTest("测试目录里没有文件")

        import time

        from imgdedup_ui.window import MainWindow

        before = self._explorer_titles()
        w = MainWindow()
        try:
            w._reveal(os.path.join(target, files[0]))
            time.sleep(3.0)
        finally:
            w.close()
        after = self._explorer_titles()

        opened = {t for t in after if "流萤表情包" in t}
        self.assertTrue(opened,
                        f"没有打开目标目录。当前资源管理器窗口：{sorted(after)}")
        # 关键回归断言：不许打开「文档」
        self.assertFalse(
            {t for t in (after - before) if t.startswith("文档")},
            "又打开了「文档」—— 说明实参又被 subprocess 转义了")


if __name__ == "__main__":
    unittest.main()
