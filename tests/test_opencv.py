"""`imgdedup_core/opencv.py`：外置 cv2 的定位、惰性代理与缺失提示。

不下载任何东西：`require()` 要么返回本机已有的 cv2，要么抛出带人话的
`ImportError`，两条路都算通过。
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

    def test_unknown_attribute_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            O.lazy_cv2.definitely_not_a_cv2_symbol_xyz


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
