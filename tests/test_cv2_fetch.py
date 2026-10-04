"""`imgdedup_core/cv2_fetch.py`：轮子挑选与解包过滤。

纯逻辑，不联网、不下载。
"""

import sys
import unittest

from imgdedup_core import cv2_fetch as F


class TestShouldSkip(unittest.TestCase):

    def test_skips_known_noise(self):
        cases = [
            ("data",),                       # haarcascade XML，6+ MiB
            ("__pycache__",),
            ("cv2", "data"),
            ("opencv_videoio_ffmpeg4100_64.dll",),
            ("cv2", "opencv_videoio_ffmpeg4100_64.dll"),
        ]
        for parts in cases:
            with self.subTest(parts=parts):
                self.assertTrue(F.should_skip(parts))

    def test_keeps_the_payload(self):
        for parts in (("cv2",), ("cv2", "cv2.pyd"), ("cv2", "config.py")):
            with self.subTest(parts=parts):
                self.assertFalse(F.should_skip(parts))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(F.should_skip(("DATA",)))
        self.assertTrue(F.should_skip(("OpenCV_videoio_ffmpeg4100.dll",)))


class TestPlatformTag(unittest.TestCase):

    def test_returns_a_windows_tag(self):
        self.assertIn(F.platform_tag(), ("win_amd64", "win32"))


class TestWheelMatches(unittest.TestCase):

    def setUp(self):
        self.plat = F.platform_tag()
        self.minor = sys.version_info.minor

    def _name(self, py: str, abi: str, plat: str | None = None) -> str:
        return f"opencv_python-4.10.0.84-{py}-{abi}-{plat or self.plat}.whl"

    def test_accepts_abi3_for_an_older_cpython(self):
        """opencv 用的是稳定 ABI：文件名写 cp37-abi3，但 3.13 照样能用。

        按 `cp313` 去过滤会得到「零个可用版本」—— 这是必须守住的一条。
        """
        self.assertTrue(F.wheel_matches(self._name("cp37", "abi3")))

    def test_rejects_abi3_from_a_newer_cpython(self):
        future = self.minor + 1
        self.assertFalse(F.wheel_matches(self._name(f"cp3{future}", "abi3")))

    def test_accepts_the_exact_cp_tag(self):
        self.assertTrue(F.wheel_matches(self._name(f"cp3{self.minor}",
                                                   f"cp3{self.minor}")))

    def test_rejects_a_different_minor(self):
        other = self.minor + 1
        self.assertFalse(F.wheel_matches(self._name(f"cp3{other}",
                                                    f"cp3{other}")))

    def test_rejects_the_wrong_platform(self):
        wrong = "win32" if self.plat == "win_amd64" else "win_amd64"
        self.assertFalse(F.wheel_matches(self._name("cp37", "abi3", plat=wrong)))

    def test_rejects_non_wheels_and_short_names(self):
        self.assertFalse(F.wheel_matches("opencv_python-4.10.0.84.tar.gz"))
        self.assertFalse(F.wheel_matches("opencv.whl"))
        self.assertFalse(F.wheel_matches(""))

    def test_accepts_a_dotted_platform_tag(self):
        # 平台标签可能写成 `linux.win_amd64` 这种点分形式，按分量比对
        self.assertTrue(F.wheel_matches(
            self._name("cp37", "abi3", plat=f"linux.{self.plat}")))


class TestConstants(unittest.TestCase):

    def test_pypi_package_name(self):
        self.assertEqual(F.PYPI_PACKAGE, "opencv-python")

    def test_skip_lists(self):
        self.assertIn("data", F.SKIP_DIR_NAMES)
        self.assertTrue(any("ffmpeg" in p for p in F.SKIP_NAME_PARTS))


if __name__ == "__main__":
    unittest.main()
