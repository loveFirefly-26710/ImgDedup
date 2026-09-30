"""`启动ImgDedup.bat`：源码启动器。

## 为什么单独给它写测试

它是用户明确要求的启动方式之一，但此前零测试覆盖。
而且这个文件历史上真出过事：被编辑器"统一行尾"成 LF 之后，
cmd 批处理解析会失步：一行被从中间劈开、后半截被当成命令执行，
连 `start` 那行都被吞掉前半截（`'pythonw.exe" -m imgdedup_ui'`），
表现是"双击没反应 + 几行『不是内部或外部命令』"。

两条硬约束（只满足一条治不好，实测过 2×2）：
  · 行尾必须 CRLF：这是主因。LF 会让 cmd 解析失步。
  · 编码必须 GBK(cp936)：与文件里的 `chcp 936` 配套。
    编码与 chcp 不配套（gbk+65001 / utf-8+936）不报错，但中文乱码。

所以绝不能用普通编辑器保存这个文件（编辑器默认写 UTF-8）。
要改就用 Python：`path.write_bytes(text.replace("\\n", "\\r\\n").encode("gbk"))`。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJ = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJ))

BAT = PROJ / "启动ImgDedup.bat"


class TestLauncherEncoding(unittest.TestCase):
    """编码与行尾：这两条错了，双击就是没反应。"""

    def test_file_exists(self):
        self.assertTrue(BAT.is_file(), f"缺少启动器：{BAT}")

    def test_is_crlf(self):
        """主因：行尾 LF 会让 cmd 批处理解析失步。"""
        raw = BAT.read_bytes()
        lf = raw.count(b"\n")
        crlf = raw.count(b"\r\n")
        self.assertEqual(lf, crlf,
                         f"有 {lf - crlf} 个裸 LF —— cmd 会解析失步（用户报过这个症状）")
        self.assertGreater(crlf, 5)

    def test_is_gbk_not_utf8(self):
        """编码必须与文件里的 `chcp 936` 配套。"""
        raw = BAT.read_bytes()
        try:
            raw.decode("gbk")
        except UnicodeDecodeError as e:
            self.fail(f"不是合法 GBK —— 与 chcp 936 不配套：{e}")
        self.assertNotIn(b"\xef\xbb\xbf", raw, "有 UTF-8 BOM，cmd 会把它当命令")

    def test_chcp_matches_encoding(self):
        text = BAT.read_bytes().decode("gbk")
        self.assertIn("chcp 936", text,
                      "改了编码就必须同步改 chcp（不配套会乱码但不报错）")


class TestLauncherLogic(unittest.TestCase):
    def test_resolves_interpreter_in_order(self):
        """三级查找：项目内 `.venv` → 开发机常见位置 → PATH。"""
        text = BAT.read_bytes().decode("gbk")
        self.assertIn(r"%~dp0.venv\Scripts\pythonw.exe", text, "没有找项目内 .venv")
        self.assertIn("for %%I in (pythonw.exe) do set", text, "没有回退到 PATH")
        self.assertIn("-m imgdedup_ui", text, "没有以模块方式启动")

    def test_has_clear_error_when_missing(self):
        """找不到解释器时要给人话，不能静默失败。"""
        text = BAT.read_bytes().decode("gbk")
        self.assertIn(":no_pythonw", text)
        self.assertIn("没找到 pythonw.exe", text)
        self.assertIn("pause", text, "报错后要 pause，否则窗口一闪而过看不到")


class TestLauncherActuallyParses(unittest.TestCase):
    """真用 cmd 跑一遍：解析错误只有跑起来才看得见。

    做法：把 `start`（会真的拉起 GUI）和 `pause`（会挂住）都换成 `echo`
    做 dry-run 副本。`pause` 必须一起换，否则报错分支撞上 `pause`，
    实验就永远挂在那里（踩过）。
    """

    @unittest.skipUnless(os.name == "nt", "cmd 只在 Windows 上")
    def test_dry_run_parses_cleanly(self):
        text = BAT.read_bytes().decode("gbk")
        dry = text.replace('start "" "%PYW%" -m imgdedup_ui',
                           'echo [dry-run] start')
        dry = dry.replace("pause", "echo [dry-run] pause")
        self.assertNotIn("start \"\"", dry, "start 没被换成 echo")
        self.assertNotIn("\npause", dry, "pause 没被换成 echo")

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="imgdedup_bat_"))
        try:
            # 副本要放在项目里：脚本里有 `cd /d "%~dp0"`，放别处就找不着模块。
            # 保留文件真实的行尾（不要 normalize 成 CRLF）——
            # 这样"行尾变 LF"这个真实故障才能在这里也暴露出来：
            # 一 normalize 就测不出它，等于把故障模式测掉了。
            copy = PROJ / "_bat_dryrun_tmp.bat"
            copy.write_bytes(dry.encode("gbk"))
            try:
                r = subprocess.run(["cmd", "/c", copy.name], cwd=str(PROJ),
                                   capture_output=True, timeout=120)
            finally:
                copy.unlink(missing_ok=True)
            out = r.stdout.decode("gbk", errors="replace")

            self.assertNotIn("不是内部或外部命令", out,
                             f"cmd 解析失步：\n{out}")
            self.assertNotIn("命令语法不正确", out, f"cmd 语法错误：\n{out}")
            self.assertEqual(r.returncode, 0, f"退出码 {r.returncode}：\n{out}")
            # 中文行必须原样出现（乱码会让这条红）
            self.assertIn("正在启动，请稍候", out, "中文行乱码或被吞掉了")
            self.assertIn("[dry-run] start", out, "没走到启动那一行")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
