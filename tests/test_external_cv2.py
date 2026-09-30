"""外置组件 cv2 的定位、加载与提示。

cv2 的 `cv2.pyd` 一个文件 86 MB（占程序 21.7%），所以不随包分发：
用户单独下载后放到 `<exe 同级>/extra/cv2/`。

这块最容易"悄悄退化"的地方：
  · 有人图省事又在顶层写 `import cv2` → 缺组件时程序连窗口都建不出来，
    用户只看到闪退、没有任何提示（这正是我们把它改成惰性代理的原因）；
  · 路径约定被改到别处 → 用户按文档放好了文件，程序却找不到。

所以两条都要钉住：顶层不许出现裸 `import cv2`，路径只能是 extra/cv2。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

PROJ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJ))

CORE = PROJ / "imgdedup_core"
MAIN = PROJ / "imgdedup_ui" / "__main__.py"
#: 启动期逻辑（图标 / cv2 检查与下载）的真身在这个普通模块里，
#: 不在 `__main__.py`：入口脚本冻结后不可导入，探针进不去，
#: 那条链路就没法在产物里验证。断言要指向这里。
SYMS = PROJ / "imgdedup_ui" / "_entry_symbols.py"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestNoHardCv2Import(unittest.TestCase):
    """业务模块顶层不许出现裸 `import cv2`。

    这是"缺组件时整个程序起不来"的根因：顶层导入失败 →
    `import imgdedup_core.embed` 就炸 → 窗口建不出来 → 用户看到无提示闪退。
    必须走惰性代理，让"缺组件"变成一句能看懂的话。
    """

    def test_core_and_ui_have_no_bare_cv2_import(self):
        offenders = []
        for f in list(CORE.glob("*.py")) + list((PROJ / "imgdedup_ui").glob("*.py")):
            if f.name == "opencv.py":
                continue          # 它自己负责真正导入
            for i, line in enumerate(read(f).splitlines(), 1):
                s = line.strip()
                if s in ("import cv2", "from cv2 import *") or \
                        s.startswith("from cv2."):
                    offenders.append(f"{f.name}:{i}  {s}")
        self.assertEqual(
            offenders, [],
            "顶层又出现裸 import cv2 了 —— 缺组件时程序会无提示闪退：\n  "
            + "\n  ".join(offenders))

    def test_business_modules_use_the_lazy_proxy(self):
        for name in ("embed.py", "feature.py"):
            src = read(CORE / name)
            self.assertIn("lazy_cv2", src,
                          f"{name} 没有走惰性代理")
            self.assertNotIn("\nimport cv2", src)


class TestPathConvention(unittest.TestCase):
    """路径约定：`<exe 同级>/extra/cv2/`。改了就要同步改文档和提示文案。"""

    @classmethod
    def setUpClass(cls):
        from imgdedup_core import opencv as O

        cls.O = O

    def test_cv2_dir_is_extra_cv2(self):
        d = self.O.cv2_dir()
        self.assertEqual(d.name, "cv2")
        self.assertEqual(d.parent.name, "extra")

    def test_extra_is_next_to_exe(self):
        """`extra` 必须和 exe 同级，不能塞进 `_internal/`。

        `_internal/` 是打包器私有目录：用户不该往里翻，
        而且升级程序时会被整个覆盖掉：用户放进去的组件会莫名消失。
        """
        d = self.O.extra_dir()
        exe_dir = Path(sys.executable).resolve().parent
        self.assertEqual(d.parent, exe_dir)
        self.assertNotIn("_internal", d.parts)

    def test_candidates_put_extra_first(self):
        """`<exe同级>/extra` 必须是第一优先。

        site-packages 里可能装着一个旧版 cv2；用户特意放进 extra 的那份
        应该赢，否则"我明明放了新的，怎么还是旧的"。
        """
        roots = self.O.candidate_roots()
        self.assertTrue(roots, "候选目录不能为空")
        self.assertEqual(roots[0], self.O.extra_dir())

    def test_install_path_hook_prepends(self):
        before = list(sys.path)
        try:
            sys.path[:] = [p for p in sys.path
                           if Path(p or ".").name != self.O.EXTRA_DIRNAME]
            self.O.install_path_hook()
            got = [Path(p) for p in sys.path[:len(self.O.candidate_roots())]]
            self.assertEqual(got, self.O.candidate_roots())
        finally:
            sys.path[:] = before

    def test_install_path_hook_is_idempotent(self):
        before = list(sys.path)
        try:
            self.O.install_path_hook()
            once = list(sys.path)
            self.O.install_path_hook()
            self.assertEqual(sys.path, once, "重复调用会一直往 sys.path 里塞")
        finally:
            sys.path[:] = before


class TestMissingHint(unittest.TestCase):
    """缺组件时给用户的说明必须能照着做：有绝对路径、有文件名、有获取方式。"""

    @classmethod
    def setUpClass(cls):
        from imgdedup_core import opencv as O

        cls.hint = O.missing_hint()

    def test_hint_has_the_exact_target_path(self):
        from imgdedup_core import opencv as O

        self.assertIn(str(O.cv2_dir()), self.hint,
                      "提示里没有「该放到哪」的完整路径")

    def test_hint_names_the_file_to_look_for(self):
        self.assertIn("cv2.pyd", self.hint,
                      "提示里要说明「里面应当看到 cv2.pyd」，"
                      "否则用户不知道自己放对没有")

    def test_hint_says_how_to_get_it(self):
        for token in ("下载", "whl"):
            self.assertIn(token, self.hint, f"提示里没说怎么获取（缺 {token}）")

    def test_hint_explains_why_it_is_separate(self):
        """要说明"为什么不在包里"，否则用户会觉得是软件坏了。"""
        self.assertIn("86", self.hint)


class TestLazyProxy(unittest.TestCase):
    """惰性代理：可用时透传，不可用时抛带说明的错误（而不是 AttributeError）。"""

    @classmethod
    def setUpClass(cls):
        from imgdedup_core import opencv as O

        cls.O = O

    def setUp(self):
        self._saved = (self.O._loaded, self.O._failed_reason)

    def tearDown(self):
        self.O._loaded, self.O._failed_reason = self._saved

    def test_proxies_attributes_when_available(self):
        self.O._loaded, self.O._failed_reason = None, None
        if not self.O.is_available():
            self.skipTest("本机没有 cv2 —— 它是外置可选组件，缺了不算错")
        # 属性透传：业务代码照旧写 cv2.resize / cv2.IMREAD_COLOR
        self.assertTrue(hasattr(self.O.lazy_cv2, "resize"))
        self.assertTrue(hasattr(self.O.lazy_cv2, "imdecode"))

    def test_raises_import_error_with_guidance_when_missing(self):
        """缺组件时抛的必须是带操作说明的 ImportError。

        抛 `AttributeError` 的话，用户看到的是 `'NoneType' has no attribute
        'resize'` 这种天书；而且 `cache.py` 里那个 `except Exception`
        会把它静默吞掉，缓存悄悄失效：排查起来非常痛苦。
        """
        self.O._loaded = None
        self.O._failed_reason = self.O.missing_hint()
        with self.assertRaises(ImportError) as cm:
            _ = self.O.lazy_cv2.resize
        self.assertIn(str(self.O.cv2_dir()), str(cm.exception))

    def test_is_available_does_not_raise(self):
        self.O._loaded = None
        self.O._failed_reason = self.O.missing_hint()
        self.assertFalse(self.O.is_available())      # 不能抛

    def test_version_returns_none_when_missing(self):
        self.O._loaded = None
        self.O._failed_reason = self.O.missing_hint()
        self.assertIsNone(self.O.version())

    def test_found_at_returns_none_when_missing(self):
        self.O._loaded = None
        self.O._failed_reason = self.O.missing_hint()
        self.assertIsNone(self.O.found_at())


class TestSpecExcludesCv2(unittest.TestCase):
    """spec 必须真的把 cv2 排除掉：不然 86 MB 又被打回来。"""

    def test_cv2_in_python_excludes(self):
        src = read(PROJ / "imgdedup.spec")
        idx = src.find("excludes = [")
        self.assertGreater(idx, -1)
        block = src[idx:src.find("]", idx)]
        self.assertIn('"cv2"', block,
                      "spec 的 excludes 里没有 cv2 —— 86 MB 会被打回包里")

    def test_cv2_binary_is_filtered(self):
        src = read(PROJ / "imgdedup.spec")
        self.assertIn("cv2.pyd", src,
                      "没有按文件名过滤 cv2.pyd（hook 会绕过 excludes 直接塞）")

    def test_numpy_core_multiarray_is_collected(self):
        """必须显式收集 `numpy.core.multiarray`，否则外置 cv2 加载不了。

        `cv2/__init__.py` 第 12 行是 `import numpy.core.multiarray`
        （numpy 1.x 的旧路径）。numpy 2.x 里它只是个转发到
        `numpy._core.multiarray` 的小垫片，而我们自己的代码从不 import 它。

        这个坑的隐蔽之处：把 cv2 排除出包之前，正是"cv2 自己 import 了它"
        顺带把它带进来的：一排除 cv2，它也一起消失了，
        表现成"外置 cv2 位置放对了却还是加载失败"，
        报错是 `ModuleNotFoundError: No module named 'numpy.core.multiarray'`。
        """
        src = read(PROJ / "imgdedup.spec")
        self.assertIn("numpy.core.multiarray", src,
                      "spec 的 hiddenimports 里少了 numpy.core.multiarray —— "
                      "外置 cv2 会加载失败（报 numpy.core.multiarray 找不到）")
        # 必须在 hiddenimports 里，不能只是注释提了一句
        idx = src.find("hiddenimports=[")
        self.assertGreater(idx, -1)
        block = src[idx:src.find("]", idx)]
        self.assertIn('"numpy.core.multiarray"', block)


class TestFetchScript(unittest.TestCase):
    """准备外置组件的脚本。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PROJ))
        from dev import fetch_cv2 as F

        cls.F = F

    def test_skips_ffmpeg_and_face_data(self):
        """必须跳过 cv2 自带的 ffmpeg 解码器与人脸 XML。

        ffmpeg 两个 DLL 共 51 MiB、人脸 XML 6+ MiB：本工具都用不到。
        打包时也是排除的，外置这份必须一致，否则"独立出来反而更大"。
        """
        self.assertTrue(self.F._skip("opencv_videoio_ffmpeg4110_64.dll"))
        self.assertTrue(self.F._skip("data"))
        self.assertTrue(self.F._skip("__pycache__"))
        self.assertFalse(self.F._skip("cv2.pyd"))
        self.assertFalse(self.F._skip("__init__.py"))
        self.assertFalse(self.F._skip("config-3.py"))

    def test_default_out_is_extra_of_onedir(self):
        self.assertEqual(self.F.DEFAULT_OUT.name, "extra")
        self.assertEqual(self.F.DEFAULT_OUT.parent.name, "ImgDedup")


class TestAppStartupGuard(unittest.TestCase):
    """启动守卫：缺组件时给对话框、返回非零码，不能崩。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_guard_returns_true_when_available(self):
        from imgdedup_ui._entry_symbols import _require_cv2

        from imgdedup_core import opencv as O

        if not O.is_available():
            self.skipTest("本机没有 cv2")
        self.assertTrue(_require_cv2(self.app))

    def test_reveal_path_does_not_raise_on_missing(self):
        """路径不存在时要静默跳过：不能因为"打不开文件夹"就拦着退出。"""
        from imgdedup_ui._entry_symbols import _reveal_path

        _reveal_path(str(PROJ / "no_such_dir_xyz"))
        _reveal_path("")


class TestWheelSelection(unittest.TestCase):
    """选 wheel 的标签匹配：这里踩过一个会让功能完全不可用的坑。

    opencv 的 wheel 用的是稳定 ABI：

        opencv_python-4.11.0.86-cp37-abi3-win_amd64.whl

    `cp37-abi3` = "CPython 3.7 及以上都能用"，所以文件名里永远不会
    出现 `cp313`。按 `cp313` 过滤会得到"零个可用版本"，
    而 PyPI 上其实有几十个：报错还会误导成"这个 Python 版本还没出包"。
    """

    @classmethod
    def setUpClass(cls):
        from imgdedup_core import cv2_fetch as F

        cls.F = F

    def test_stable_abi_wheel_matches(self):
        """本机实际在用的那种命名（`cp37-abi3-win_amd64`）必须匹配上。"""
        self.assertTrue(
            self.F.wheel_matches(
                "opencv_python-4.11.0.86-cp37-abi3-win_amd64.whl"))

    def test_newer_stable_abi_also_matches(self):
        self.assertTrue(
            self.F.wheel_matches("opencv_python-5.0.0.93-cp37-abi3-win_amd64.whl"))

    def test_other_platforms_are_rejected(self):
        for name in (
            "opencv_python-4.11.0.86-cp37-abi3-win32.whl",
            "opencv_python-4.11.0.86-cp37-abi3-manylinux_2_17_x86_64.whl",
            "opencv_python-5.0.0.93-cp37-abi3-macosx_13_0_arm64.whl",
        ):
            self.assertFalse(self.F.wheel_matches(name), name)

    def test_sdist_is_rejected(self):
        self.assertFalse(
            self.F.wheel_matches("opencv-python-4.11.0.86.tar.gz"))

    def test_exact_cpython_tag_still_works(self):
        """不是 abi3、而是精确版本的轮子（别的包常见）也要认。"""
        import sys

        cur = f"cp{sys.version_info.major}{sys.version_info.minor}"
        self.assertTrue(
            self.F.wheel_matches(f"foo-1.0-{cur}-{cur}-win_amd64.whl"))
        self.assertFalse(
            self.F.wheel_matches("foo-1.0-cp39-cp39-win_amd64.whl"))

    def test_abi3_newer_than_this_python_is_rejected(self):
        """`cp399-abi3` 这种"未来版本"的稳定 ABI 不能装。"""
        self.assertFalse(
            self.F.wheel_matches("foo-1.0-cp399-abi3-win_amd64.whl"))


class TestSkipRules(unittest.TestCase):
    """筛选规则必须只有一份（打包时排除什么、外置就跳过什么）。"""

    @classmethod
    def setUpClass(cls):
        from imgdedup_core import cv2_fetch as F

        cls.F = F

    def test_skips_ffmpeg_and_face_data(self):
        self.assertTrue(self.F.should_skip(("opencv_videoio_ffmpeg4110_64.dll",)))
        self.assertTrue(self.F.should_skip(("data", "haarcascade_x.xml")))
        self.assertTrue(self.F.should_skip(("__pycache__",)))

    def test_keeps_what_is_needed(self):
        for rel in (("cv2.pyd",), ("__init__.py",), ("config-3.py",),
                    ("aruco", "__init__.pyi")):
            self.assertFalse(self.F.should_skip(rel), rel)

    def test_dev_script_delegates_to_core(self):
        """`dev/fetch_cv2.py` 必须委托给 core，不能自己再写一套。

        两套规则迟早会分叉：那样"程序自动下载的"和"打包的人准备的"
        会是不同的东西。
        """
        from dev import fetch_cv2 as D

        for name in ("opencv_videoio_ffmpeg4110_64.dll", "data",
                     "__pycache__", "cv2.pyd", "__init__.py"):
            self.assertEqual(D._skip(name), self.F.should_skip((name,)), name)


class TestRefreshAfterInstall(unittest.TestCase):
    """装完组件必须能"重新判断"，否则刚下好也继续报缺组件。"""

    @classmethod
    def setUpClass(cls):
        from imgdedup_core import opencv as O

        cls.O = O

    def setUp(self):
        self._saved = (self.O._loaded, self.O._failed_reason)

    def tearDown(self):
        self.O._loaded, self.O._failed_reason = self._saved

    def test_refresh_clears_cached_failure(self):
        """缓存里记着"没有"时，refresh() 要能重新探测成功。

        不 refresh 的话，用户刚把 cv2 装好、程序仍报缺组件 ——
        用户会以为"下载了也没用"。
        """
        self.O._loaded = None
        self.O._failed_reason = "假装之前失败过"
        if not self.O.refresh():
            self.skipTest("本机没有 cv2 —— 它是外置可选组件，缺了不算错")
        self.assertIsNone(self.O._failed_reason)


class TestAutoDownloadIsWired(unittest.TestCase):
    """对话框里承诺了「自动下载」，就必须真的提供它。

    这条是因为踩过才加的：第一版提示文案写着
    "程序会问你要不要自动下载，点「是」即可"，而对话框里只有
    「打开该文件夹」和「退出」—— 承诺了一个不存在的功能。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_offers_download_button(self):
        src = read(SYMS)
        self.assertIn('addButton("自动下载"', src,
                      "缺 cv2 的对话框里没有「自动下载」按钮")
        self.assertIn("_download_cv2", src)

    def test_download_flow_is_in_an_importable_module(self):
        """这段逻辑必须在普通模块里，不能留在入口脚本。

        入口脚本（`__main__.py`）冻结后不是可导入模块，探针
        `import imgdedup_ui.__main__` 会 `ModuleNotFoundError` ——
        于是"在冻结产物里跑一遍自动下载"这件事根本做不到。
        （本轮写探针时就是这么被挡住的。）
        """
        syms = read(SYMS)
        for name in ("class _Cv2DownloadTask", "def _download_cv2",
                     "def _require_cv2"):
            self.assertIn(name, syms, f"{name} 不在可导入模块里")
        main = read(MAIN)
        self.assertIn("from imgdedup_ui._entry_symbols import _require_cv2",
                      main, "入口脚本没有转发")
        self.assertNotIn("def _download_cv2", main,
                         "入口脚本里又出现实现了（冻结后验不了）")

    def test_was_canceled_is_read_before_close(self):
        """`wasCanceled()` 必须在 `close()` 之前读：这是真踩过的坑。

        `QProgressDialog.close()` 本身就会发出 `canceled` 信号
        （Qt 的既定行为：关闭进度对话框 = 取消）。于是：

            close() → canceled → wasCanceled() 变 True

        结果：下载成功了也会被判成"用户取消"，永远返回失败。
        用户看到的是"下载失败"，而文件其实已经下好了。

        这个 bug 只有真的把那段代码跑一遍才发现 ——
        所以这里用顺序断言守住（联网测试太慢，不进默认套件）。
        """
        src = read(SYMS)
        idx = src.find("def _download_cv2")
        self.assertGreater(idx, -1)
        body = src[idx:src.find("\ndef ", idx + 10)]

        # 锚点必须是整条语句，不能是 `wasCanceled()` 这种裸名字。
        # 上面那段解释性的注释里也写了 `wasCanceled()`，
        # 用裸名字会命中注释、让这条断言恒真（第一版就是这么写的，漏报了）。
        read_stmt = "user_canceled = box.wasCanceled()"
        read_at = body.find(read_stmt)
        close_at = body.find("box.close()")
        self.assertGreater(read_at, -1, f"没有找到 `{read_stmt}`")
        self.assertGreater(close_at, -1, "没有 box.close()")
        self.assertLess(
            read_at, close_at,
            "`wasCanceled()` 读在 `box.close()` 之后了 —— "
            "close() 会触发 canceled，导致下载成功也报失败")

    def test_download_task_uses_the_shared_core(self):
        """下载逻辑必须走 `imgdedup_core.cv2_fetch`，别在这里另写一套。"""
        src = read(SYMS)
        self.assertIn("cv2_fetch", src)

    def test_download_runs_off_the_ui_thread(self):
        """下载 42 MB，不能在主线程同步做，否则界面直接卡死。

        判据：下载任务是个 `QThread` 子类，且用真 `QEventLoop` 等它
        （不能用 `while: processEvents()` 忙轮询驱动 Qt，会饿死定时器）。
        """
        src = read(SYMS)
        self.assertIn("class _Cv2DownloadTask(QThread)", src)
        self.assertIn("loop.exec()", src)

    def test_hint_no_longer_promises_something_else(self):
        """提示文案要与实际行为一致（提到「自动下载」是对的，因为它现在有了）。"""
        from imgdedup_core import opencv as O

        self.assertIn("自动下载", O.missing_hint())


class TestDownloadFlowEndToEnd(unittest.TestCase):
    """真的走一遍「自动下载」的 Qt 链路（进度对话框 + 后台线程 + 事件循环）。

    为什么需要：前面那些测试只断言"按钮存在""用的是 QThread"，
    但这段代码从没被执行过，而它恰恰是最容易出问题的地方：
      · `QProgressDialog` + `QEventLoop` 的组合写错会永久卡住；
      · 线程没 `wait()` 就离开作用域会让 Qt abort（同 GIF 那个坑）。

    默认跳过（要下载 42 MB）。设 `IMGDEDUP_NET_TESTS=1` 才跑。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_download_flow_completes_and_installs(self):
        import os

        if os.environ.get("IMGDEDUP_NET_TESTS") != "1":
            self.skipTest("联网测试默认跳过（设 IMGDEDUP_NET_TESTS=1 才跑）")

        import tempfile
        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        from imgdedup_ui.__main__ import _download_cv2

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "extra"
            # 末尾那两个弹窗会阻塞等人点，离屏环境没人点 → 永久挂住。
            # 我们验的是"下载+解压+进度"这段，弹窗单独用源码断言守。
            with mock.patch.object(QMessageBox, "information",
                                   return_value=QMessageBox.Ok), \
                    mock.patch.object(QMessageBox, "critical",
                                      return_value=QMessageBox.Ok):
                ok = _download_cv2(self.app, target)
            self.assertTrue(ok, "下载流程返回失败")
            self.assertTrue((target / "cv2" / "cv2.pyd").is_file(),
                            "下载完了但 cv2.pyd 不在位")
            # 跳过规则也要在真实下载里生效
            self.assertFalse(list((target / "cv2").glob("*ffmpeg*")),
                             "把 ffmpeg 解码器（51 MB）也下下来了")
            self.assertFalse((target / "cv2" / "data").exists(),
                             "把人脸检测 XML 也下下来了")

    def test_downloaded_cv2_is_importable_from_that_dir(self):
        """下载下来的那份必须真的能用：不只是"文件在那儿"。

        这条单独跑一个子进程：要在干净的解释器里只把该目录挂上 sys.path，
        证明它自包含（不依赖本机已装的 cv2）。
        """
        import os

        if os.environ.get("IMGDEDUP_NET_TESTS") != "1":
            self.skipTest("联网测试默认跳过（设 IMGDEDUP_NET_TESTS=1 才跑）")

        import subprocess
        import tempfile
        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        from imgdedup_ui.__main__ import _download_cv2

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "extra"
            with mock.patch.object(QMessageBox, "information",
                                   return_value=QMessageBox.Ok), \
                    mock.patch.object(QMessageBox, "critical",
                                      return_value=QMessageBox.Ok):
                if not _download_cv2(self.app, target):
                    self.skipTest("下载失败，跳过（上一条测试会报）")

            code = (
                "import sys; sys.path.insert(0, r'%s')\n"
                "import cv2, numpy as np\n"
                "assert r'%s' in cv2.__file__, cv2.__file__\n"
                "img = np.zeros((8,8,3), np.uint8)\n"
                "assert cv2.resize(img,(4,4)).shape == (4,4,3)\n"
                "assert cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).shape == (8,8)\n"
                "ok, buf = cv2.imencode('.webp', img)\n"
                "assert ok and len(buf) > 0\n"
                "print('CV2_OK', cv2.__version__)\n"
            ) % (str(target), str(target))
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0,
                             f"下载的 cv2 加载失败：\n{r.stderr[-1200:]}")
            self.assertIn("CV2_OK", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
