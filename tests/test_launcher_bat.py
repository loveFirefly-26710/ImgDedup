"""`启动ImgDedup.bat` 的三条硬约束：编码、行尾、真能跑。

cmd 批处理对这两件事极其敏感：
- 存成 UTF-8 会让带中文的行解析失步（一行被从中间劈开，后半截被当命令执行）；
- 存成 LF 会直接解析错乱，表现是「双击没反应」。

所以这份文件只能 GBK + CRLF，而且必须真的过一遍 cmd。
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT = os.path.join(ROOT, "启动ImgDedup.bat")


def _raw() -> bytes:
    with open(BAT, "rb") as fh:
        return fh.read()


class TestEncoding(unittest.TestCase):

    def test_no_bom(self):
        raw = _raw()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "不能有 UTF-8 BOM")
        self.assertFalse(raw.startswith(b"\xff\xfe"), "不能有 UTF-16 BOM")

    def test_decodes_as_gbk(self):
        text = _raw().decode("gbk")
        self.assertIn("图片去重", text)

    def test_would_be_mojibake_as_utf8(self):
        """反证：同一份字节按 UTF-8 解出来不是正常中文。

        这条挡的是「有人用编辑器把它另存为 UTF-8」—— 那时候上面那条
        GBK 断言会红，但这条能直接说明原因。
        """
        try:
            as_utf8 = _raw().decode("utf-8")
        except UnicodeDecodeError:
            return
        self.assertNotIn("图片去重", as_utf8)


class TestLineEndings(unittest.TestCase):

    def test_every_newline_is_crlf(self):
        raw = _raw()
        bare = raw.replace(b"\r\n", b"").count(b"\n")
        self.assertEqual(bare, 0, f"有 {bare} 个裸 LF —— cmd 会解析失步")

    def test_has_crlf(self):
        self.assertIn(b"\r\n", _raw())


class TestContents(unittest.TestCase):

    def setUp(self):
        self.text = _raw().decode("gbk")

    def test_sets_the_code_page(self):
        self.assertIn("chcp 936", self.text)

    def test_three_level_interpreter_lookup(self):
        # .venv → 开发机固定位置 → PATH；再加开头那次清空，一共 4 次赋值
        self.assertIn(r"%~dp0.venv\Scripts\pythonw.exe", self.text)
        self.assertIn("%%~$PATH:I", self.text)
        self.assertEqual(self.text.count('set "PYW='), 4)

    def test_launches_the_package(self):
        self.assertIn('-m imgdedup_ui', self.text)

    def test_exit_codes_distinguish_success_from_failure(self):
        self.assertIn("exit /b 0", self.text)
        self.assertIn("exit /b 1", self.text)

    def test_failure_path_pauses_so_the_user_can_read_it(self):
        tail = self.text[self.text.index(":no_pythonw"):]
        self.assertIn("pause", tail)


@unittest.skipUnless(sys.platform == "win32", "cmd 只在 Windows 上有")
class TestCmdActuallyParsesIt(unittest.TestCase):

    def test_dry_run_has_no_parse_errors(self):
        """把 `start` / `pause` 换成 `echo` 做一份副本，用 cmd 真跑一遍。

        只做静态断言的话，「行尾错了导致半行被当命令执行」这类问题抓不到 ——
        那种情况下 cmd 会打印一条 `'xxx' 不是内部或外部命令`，而退出码仍是 0。
        """
        text = _raw().decode("gbk")
        text = text.replace('start "" "%PYW%" -m imgdedup_ui',
                            'echo DRYRUN-OK')
        text = text.replace("pause", "echo DRYRUN-PAUSE")

        with tempfile.TemporaryDirectory(prefix="imgdedup_bat_") as tmp:
            path = os.path.join(tmp, "dryrun.bat")
            with open(path, "wb") as fh:
                fh.write(text.encode("gbk"))
            r = subprocess.run(["cmd", "/c", path], capture_output=True,
                               timeout=120)
        out = r.stdout.decode("gbk", "replace")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("DRYRUN-OK", out)
        self.assertIn("ImgDedup", out)
        self.assertNotIn("不是内部或外部命令", out)
        self.assertNotIn("The system cannot find", out)

    def _run(self, data: bytes) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="imgdedup_bat_") as tmp:
            path = os.path.join(tmp, "v.bat")
            with open(path, "wb") as fh:
                fh.write(data)
            r = subprocess.run(["cmd", "/c", path], capture_output=True,
                               timeout=120)
        return r.returncode, (r.stdout + r.stderr).decode("gbk", "replace")

    def _neutralised(self) -> str:
        text = _raw().decode("gbk")
        text = text.replace('start "" "%PYW%" -m imgdedup_ui', "echo DRYRUN-OK")
        return text.replace("pause", "echo DRYRUN-PAUSE")

    def test_lf_line_endings_break_parsing(self):
        """注入验证：改成 LF 之后 cmd 会把半行当命令执行。

        实测下来，这一条是唯一能让退出码变红的破坏方式。
        """
        rc, out = self._run(self._neutralised().replace("\r\n", "\n").encode("gbk"))
        self.assertNotEqual(rc, 0, "LF 行尾居然没让 cmd 报错")
        self.assertRegex(out, r"不是内部或外部命令|is not recognized")

    def test_utf8_bom_breaks_parsing(self):
        """注入验证：加 BOM 之后第一行的 `@echo` 会被 BOM 污染成未知命令。

        注意它不一定让退出码变红（cmd 会继续往下跑），所以判据是输出里
        出现解析错误，而不是退出码。
        """
        _rc, out = self._run(b"\xef\xbb\xbf" + self._neutralised().encode("gbk"))
        self.assertRegex(out, r"不是内部或外部命令|is not recognized")


if __name__ == "__main__":
    unittest.main()
