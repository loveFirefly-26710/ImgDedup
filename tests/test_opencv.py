"""`imgdedup_core/opencv.py`：外置 cv2 的定位、惰性代理与缺失提示。

**每个用例都要在「有 cv2」和「没有 cv2」两种环境下都过。** CI 上只装
`requirements.txt`，而 cv2 是外置组件、不在里面；开发机上通常装了 ——
于是「本地全绿、CI 全红」是这一块最容易踩的坑。写断言时按这个前提来：
凡是依赖 cv2 存在的，都要么 `skipUnless`，要么把两条分支都写出来。
"""

import sys
import unittest

from imgdedup_core import opencv as O


class TestPaths(unittest.TestCase):

    def test_dir_names(self):
        self.assertEqual(O.EXTRA_DIRNAME, "extra")
        self.assertEqual(O.CV2_DIRNAME, "cv2")
        self.assertEqual(O.extra_dir().name, "extra")
        self.assertEqual(O.cv2_dir().name, "cv2")
        self.assertEqual(O.cv2_dir().parent, O.extra_dir())

    def test_candidate_roots_are_unique_and_ordered(self):
        roots = O.candidate_roots()
        self.assertTrue(roots)
        self.assertEqual(len(roots), len(set(roots)))
        # 头两个是 exe 同级与 onedir 的 _internal
        self.assertEqual(roots[0].name, "extra")


class TestMissingHint(unittest.TestCase):

    def test_hint_is_actionable(self):
        hint = O.missing_hint()
        self.assertIsInstance(hint, str)
        self.assertIn("extra", hint)
        self.assertIn("cv2", hint)


class TestInstallPathHook(unittest.TestCase):

    def test_hook_prepends_candidates(self):
        saved = list(sys.path)
        try:
            sys.path[:] = [p for p in sys.path
                           if p not in {str(r) for r in O.candidate_roots()}]
            O.install_path_hook()
            self.assertEqual(sys.path[:1], [str(O.candidate_roots()[0])])
            before = list(sys.path)
            O.install_path_hook()          # 幂等
            self.assertEqual(sys.path, before)
        finally:
            sys.path[:] = saved


class TestLazyProxy(unittest.TestCase):

    def test_proxy_type(self):
        self.assertIsInstance(O.lazy_cv2, O._LazyCv2)

    def test_proxy_exposes_dunder_passthrough(self):
        # `__getattr__` 只处理普通属性；`repr()` 之类不该触发加载
        self.assertEqual(repr(O.lazy_cv2), "<lazy cv2 proxy>")

    def test_unknown_attribute_is_not_swallowed(self):
        """未知属性必须抛异常，不能静默返回 None。

        抛哪一种取决于 cv2 在不在：在的时候 `getattr` 抛 AttributeError
        （模块里确实没这个符号）；不在的时候 `require()` 先抛 ImportError
        （带缺组件提示）。CI 上没有 cv2，开发机上一般有 —— 两种都得接受，
        但都不能什么都不抛。
        """
        expected = AttributeError if O.is_available() else ImportError
        with self.assertRaises(expected):
            O.lazy_cv2.definitely_not_a_cv2_symbol_xyz


class TestMissingCv2(unittest.TestCase):
    """把 `require()` 打成失败，钉住「缺组件」这条路径。

    本机装了 cv2 时它是模拟路径，CI 上它是真实路径 —— 两边跑的是同一段断言。
    没有这一段的话，「缺组件」的行为只有在 CI 上才被验到，等于没验。
    """

    def setUp(self):
        self._saved = (O._loaded, O._failed_reason)
        O._loaded = None
        O._failed_reason = O.missing_hint() + "\n\n（测试打桩：本进程里 cv2 不可用）"
        self.addCleanup(self._restore)

    def _restore(self):
        O._loaded, O._failed_reason = self._saved

    def test_require_raises_import_error(self):
        with self.assertRaises(ImportError):
            O.require()

    def test_proxy_raises_import_error_not_attribute_error(self):
        # 这一条就是 CI 上曾经挂掉的那个断言
        with self.assertRaises(ImportError):
            O.lazy_cv2.cvtColor

    def test_error_message_carries_the_hint(self):
        with self.assertRaises(ImportError) as cm:
            O.lazy_cv2.cvtColor
        self.assertIn("extra", str(cm.exception))
        self.assertIn("cv2", str(cm.exception))

    def test_is_available_is_false(self):
        self.assertFalse(O.is_available())

    def test_version_and_found_at_degrade_to_none(self):
        self.assertIsNone(O.version())
        self.assertIsNone(O.found_at())

    @unittest.skipIf(O.is_available(), "本机有 cv2，refresh() 会重新探测成功")
    def test_refresh_retries_and_still_reports_unavailable(self):
        # 打桩在这里不管用：refresh() 会清掉缓存重新探测，塞进去的
        # `_failed_reason` 被抹掉，有 cv2 的机器上就重探成功了。
        # 这条只在真缺 cv2 的环境里验得到。
        self.assertFalse(O.refresh())


class TestRequire(unittest.TestCase):

    def test_either_returns_a_module_or_raises_with_a_hint(self):
        try:
            mod = O.require()
        except ImportError as e:
            self.assertIn("extra", str(e))
            self.assertIn("cv2", str(e))
        else:
            self.assertTrue(hasattr(mod, "cvtColor"))

    def test_is_available_agrees_with_require(self):
        try:
            O.require()
        except ImportError:
            self.assertFalse(O.is_available())
        else:
            self.assertTrue(O.is_available())

    def test_version_and_found_at_are_consistent(self):
        if not O.is_available():
            self.skipTest("本机没有可用的 cv2")
        self.assertIsInstance(O.version(), str)
        self.assertTrue(O.found_at())
        self.assertIsInstance(O.refresh(), bool)


@unittest.skipUnless(O.is_available(), "本机没有可用的 cv2")
class TestProxyLoads(unittest.TestCase):

    def test_attribute_access_loads_the_module(self):
        self.assertTrue(callable(O.lazy_cv2.cvtColor))


if __name__ == "__main__":
    unittest.main()
