"""打包配置的一致性：版本号单一来源、外置组件不被卷进包里、安装包不弹 UAC。

这些约束违反了不会让任何功能测试变红 —— 只会在发布产物里体现出来
（包变大 171 MB、或者多出一个 AGPL 组件）。所以只能靠静态断言钉住。
"""

import os
import tempfile
import unittest
from pathlib import Path

import imgdedup_core
from dev import check_artifacts as CA
from dev import check_deps as CD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TestVersionSingleSource(unittest.TestCase):

    def test_version_looks_like_a_version(self):
        self.assertRegex(imgdedup_core.__version__, r"^\d+\.\d+\.\d+$")

    def test_ui_package_has_no_version_copy(self):
        """界面包里不许再放一份版本号 —— 两份迟早对不上。"""
        import imgdedup_ui
        self.assertFalse(hasattr(imgdedup_ui, "__version__"))

    def test_make_installer_reads_it_from_the_core_package(self):
        src = _read("dev/make_installer.py")
        self.assertRegex(src, r'__version__\s*\\s\*=\\s\*"')
        self.assertIn("imgdedup_core", src)

    def test_installer_script_accepts_the_version_from_the_command_line(self):
        src = _read("imgdedup.iss")
        self.assertIn("/DAppVersion", src)
        self.assertIn("#ifndef AppVersion", src, "单独跑 ISCC 时要有个兜底默认值")

    def test_installer_output_name_carries_the_version(self):
        self.assertIn("OutputBaseFilename={#AppName}-{#AppVersion}-setup",
                      _read("imgdedup.iss"))


class TestSpecKeepsBigThingsOut(unittest.TestCase):

    def setUp(self):
        self.src = _read("imgdedup.spec")

    def test_cv2_is_excluded_from_python_analysis(self):
        self.assertRegex(self.src, r'excludes\s*=\s*\[[^\]]*"cv2"', )

    def test_binaries_are_filtered_by_filename_too(self):
        """`excludes` 只过滤 Python 模块；cv2 的 hook 会把 cv2.pyd 直接塞进来。"""
        block = self.src[self.src.index("EXCLUDE_BINARIES_KEYWORDS"):]
        self.assertIn("cv2.pyd", block)
        self.assertIn("opencv_videoio_ffmpeg", block)

    def test_icon_goes_into_datas(self):
        self.assertIn("ICON_ICO", self.src)
        self.assertRegex(self.src, r"datas\.append\(\(str\(ICON_ICO\)")

    def test_model_directory_is_not_bundled(self):
        for line in self.src.splitlines():
            if "MODEL_DIR" in line and ("datas" in line or "binaries" in line):
                self.fail(f"模型被塞进产物了：{line.strip()}")


class TestInstallerScript(unittest.TestCase):

    def setUp(self):
        self.src = _read("imgdedup.iss")

    def test_per_user_install_without_uac(self):
        self.assertIn("PrivilegesRequired=lowest", self.src)
        self.assertIn(r"DefaultDirName={localappdata}\Programs", self.src)

    def test_uninstall_clears_the_external_component_directory(self):
        """不清 `extra\\` 的话，卸载后会剩一个最多 171 MB 的孤儿目录。"""
        self.assertRegex(self.src, r'Type:\s*filesandordirs;\s*Name:\s*"\{app\}\\extra"')

    def test_installer_ships_the_whole_onedir_tree(self):
        self.assertRegex(self.src, r"recursesubdirs")


class TestGitignore(unittest.TestCase):

    def setUp(self):
        self.lines = [ln.strip() for ln in _read(".gitignore").splitlines()]

    def test_big_assets_are_ignored(self):
        for entry in ("resources/models/", "/extra/", ".workbuddy-ai/",
                      "audit-workspace/", ".venv/", "dist/", "build/",
                      ".scratch/", "__pycache__/"):
            with self.subTest(entry=entry):
                self.assertIn(entry, self.lines)

    def test_spec_is_not_ignored(self):
        """`*.spec` 这种通配会把手写的打包配置一起忽略掉，必须没有它。"""
        self.assertNotIn("*.spec", self.lines)


class TestDependencyNotices(unittest.TestCase):

    def test_requirements_matches_the_third_party_table(self):
        self.assertEqual(CD.check(), [])

    def test_gate_catches_a_planted_mismatch(self):
        """注入验证：往依赖清单里塞一个组件表里没有的包，门禁必须报出来。"""
        with tempfile.TemporaryDirectory(prefix="imgdedup_deps_") as tmp:
            req = os.path.join(tmp, "requirements.txt")
            with open(req, "w", encoding="utf-8") as fh:
                fh.write("definitely-not-declared==1.0.0\n")
            problems = CD.check(req_path=Path(req))
        self.assertTrue(problems)
        self.assertIn("definitely-not-declared", " ".join(problems))


class TestArtifactRedLines(unittest.TestCase):

    def test_lists_the_two_external_components_and_agpl(self):
        keys = set(CA.FORBIDDEN_INTERNAL)
        self.assertIn("cv2", keys)
        self.assertIn("pymupdf", keys)

    def test_missing_internal_is_reported(self):
        with tempfile.TemporaryDirectory(prefix="imgdedup_art_") as tmp:
            problems = CA.forbidden_entries(Path(tmp))
        self.assertTrue(problems)
        self.assertIn("_internal", problems[0])

    def test_clean_tree_passes(self):
        with tempfile.TemporaryDirectory(prefix="imgdedup_art_") as tmp:
            os.makedirs(os.path.join(tmp, "_internal"))
            self.assertEqual(CA.forbidden_entries(Path(tmp)), [])

    def test_planted_cv2_is_caught(self):
        """注入验证：把 cv2 目录塞进 `_internal`，红线扫描必须报出来。"""
        with tempfile.TemporaryDirectory(prefix="imgdedup_art_") as tmp:
            os.makedirs(os.path.join(tmp, "_internal", "cv2"))
            bad = CA.forbidden_entries(Path(tmp))
        self.assertTrue(bad)
        self.assertIn("cv2", " ".join(bad))


class TestIconAssets(unittest.TestCase):

    def test_icons_exist(self):
        for rel in ("resources/icon.ico", "resources/icon.png"):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)))

    def test_ico_carries_several_sizes(self):
        """任务栏、资源管理器、Alt-Tab 各取一档，只做一个尺寸会在某处糊掉。"""
        with open(os.path.join(ROOT, "resources", "icon.ico"), "rb") as fh:
            raw = fh.read()
        n = int.from_bytes(raw[4:6], "little")
        self.assertGreaterEqual(n, 4, f"ico 里只有 {n} 个尺寸")


class TestMakeInstallerCli(unittest.TestCase):

    def test_build_and_portable_are_mutually_exclusive(self):
        src = _read("dev/make_installer.py")
        self.assertIn("add_mutually_exclusive_group()", src)
        group = src[src.index("add_mutually_exclusive_group()"):]
        group = group[:group.index("ap.add_argument")]
        self.assertIn('"--build"', group)
        self.assertIn('"--portable"', group)

    def test_check_flag_is_separate(self):
        src = _read("dev/make_installer.py")
        self.assertRegex(src, r'ap\.add_argument\(\s*"--check"')


if __name__ == "__main__":
    unittest.main()
