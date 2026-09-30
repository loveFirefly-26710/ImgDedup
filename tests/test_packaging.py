"""打包配置的测试。

打包这块最容易「悄悄退化」：有人把二进制裁剪删了，体积从 403 涨回 582，
或者把 spec 里的模型断言删了，打出个"看起来正常但语义层用不了"的包。
这些都不会让任何功能测试失败，所以必须单独钉住。
"""

import io
import os
import re
import sys
import unittest
from pathlib import Path

PROJ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJ))

SPEC = PROJ / "imgdedup.spec"
MAIN = PROJ / "imgdedup_ui" / "__main__.py"
GITIGNORE = PROJ / ".gitignore"
ISS = PROJ / "imgdedup.iss"
ICON_ICO = PROJ / "resources" / "icon.ico"
ICON_PNG = PROJ / "resources" / "icon.png"

MAKE_INSTALLER = PROJ / "dev" / "make_installer.py"


def read(p: Path) -> str:
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


class TestSpec(unittest.TestCase):
    def test_spec_exists(self):
        self.assertTrue(SPEC.is_file(), "缺少打包配置 imgdedup.spec")

    def test_model_is_not_bundled(self):
        """模型是外置组件，不能被打进产物。

        这条是从反方向钉的。早先的设计是"模型随包分发"，
        `datas` 里有一句 `(str(MODEL_DIR), "models")`，安装包因此多 85 MB。
        现在模型和 cv2 一样改成外置（`<exe 同级>/extra/models`）——
        谁再把模型塞回 `datas`，安装包体积会立刻反弹，这里拦住它。
        """
        src = read(SPEC)
        self.assertNotIn('(str(MODEL_DIR), "models")', src,
                         "模型又被塞进 datas 了 —— 它应该是外置组件")
        self.assertIn("datas = []", src,
                      "datas 只该由后面追加的图标等组成，不该含模型")

    def test_spec_does_not_fail_when_model_missing(self):
        """模型缺失不能让打包失败。

        早先是反过来的：spec 里有一条"缺模型就 `raise SystemExit`"的兜底。
        模型外置之后，缺模型是发布形态的正常状态（用户自己放 / 程序里
        自动下载），再留着那条断言会让 CI 上没取模型的构建直接红掉。
        特征就是那句提示「先跑： python -m dev.fetch_model」。
        """
        src = read(SPEC)
        self.assertNotIn("dev.fetch_model", src,
                         "spec 里还有'缺模型就退出'的断言，会让 CI 直接红")
        self.assertNotIn("MODEL_DIR.is_dir()", src)

    def test_binary_trimming_present(self):
        """`excludes` 只过滤 Python 模块，DLL 必须另按文件名过滤。"""
        src = read(SPEC)
        self.assertIn("EXCLUDE_BINARIES_KEYWORDS", src)
        self.assertIn("_keep_binary", src)
        self.assertIn("a.binaries = [", src)

    def test_known_big_and_useless_binaries_are_trimmed(self):
        src = read(SPEC)
        for token in ("opencv_videoio_ffmpeg", "opengl32sw", "Qt6Quick"):
            self.assertIn(token, src, f"没有裁掉 {token}")

    def test_useless_python_stacks_are_excluded(self):
        src = read(SPEC)
        for token in ("scipy", "pandas"):
            self.assertIn(f'"{token}"', src, f"没有排除 {token}")


class TestExtraReadme(unittest.TestCase):
    """免安装版 `extra/` 里那份说明文件。"""

    def test_no_bare_cr_in_generated_readme(self):
        """说明文件不能出现裸 CR（`\\r\\r\\n`）。

        `Path.write_text` 默认会把串里的 `\\n` 再转成 `\\r\\n`（Windows），
        而串里已经手写了 `\\r\\n`：结果变成 `\\r\\r\\n`，
        用记事本打开每行多一个空行。

        这个 bug 原来就有：是打完包去数产物字节（33 个裸 CR）才发现的，
        光看渲染出来的文字看不出问题（多数编辑器会把 `\\r\\r\\n` 折成一个换行）。
        """
        import tempfile
        from unittest import mock

        sys.path.insert(0, str(PROJ))
        from dev import make_installer as M

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(M, "ONEDIR", Path(td)):
            extra = M.ensure_extra_dir()
            readme = extra / "外置组件放这里.txt"
            self.assertTrue(readme.is_file(), "没生成说明文件")
            raw = readme.read_bytes()

        bare = raw.count(b"\r") - raw.count(b"\r\n")
        self.assertEqual(bare, 0, f"出现了 {bare} 个裸 CR（\\r\\r\\n）")
        self.assertGreater(raw.count(b"\r\n"), 5, "行尾不是 CRLF？")


class TestDocsConsistency(unittest.TestCase):
    """文档里那些"会随代码变动的数字"必须和实际对得上。

    这个漂移在一次会话里就踩了两次（386 → 391 → 393）：
    每加一个测试，文档里的项数就过期。
    人肉同步迟早会漏，所以让测试自己盯着。
    """

    def _actual_test_count(self) -> int:
        loader = unittest.TestLoader()
        suite = loader.discover(str(PROJ / "tests"), pattern="test_*.py")
        return suite.countTestCases()

    def test_doc_test_count_matches(self):
        """项数那句话现在写在开发文档里（README 只留用户向内容）。"""
        n = self._actual_test_count()
        src = read(PROJ / "docs" / "开发文档.md")
        m = re.search(r"(\d+)\s*项测试", src)
        self.assertIsNotNone(m, "开发文档里找不到『N 项测试』这句话")
        self.assertEqual(
            int(m.group(1)), n,
            f"开发文档说 {m.group(1)} 项测试，实际 {n} 项 —— 改一下开发文档")


class TestThresholdConsistency(unittest.TestCase):
    """相似度默认值只能有一个真相来源。

    历史问题：GUI 滑块给的是 0.92（滑块的"中间值"），而引擎侧
    `embed.DEFAULT_THRESHOLD` 与 `ScanWorker` 的默认参数都是 0.97 ——
    于是"文档说 0.97、界面默认 0.92"，用户一上手就落在最容易过合并的那一档
    （实测 0.92 在这个图库上合并出 1554 张的巨型组）。
    """

    def test_gui_slider_default_matches_engine_threshold(self):
        eng = read(PROJ / "imgdedup_core" / "embed.py")
        m = re.search(r"^DEFAULT_THRESHOLD\s*=\s*([0-9.]+)", eng, re.M)
        self.assertIsNotNone(m, "embed.py 里找不到 DEFAULT_THRESHOLD")

        ui = read(PROJ / "imgdedup_ui" / "window.py")
        m2 = re.search(r"^SIM_DEFAULT_PCT\s*=\s*([0-9]+)", ui, re.M)
        self.assertIsNotNone(m2, "window.py 里找不到 SIM_DEFAULT_PCT")

        self.assertEqual(
            int(m2.group(1)), round(float(m.group(1)) * 100),
            "GUI 滑块默认值与 imgdedup_core.embed.DEFAULT_THRESHOLD 不一致")
        self.assertIn("self.sld.setValue(SIM_DEFAULT_PCT)", ui,
                      "滑块必须用 SIM_DEFAULT_PCT，别写死数字")


class TestSelftestEntry(unittest.TestCase):
    def test_main_supports_selftest_flag(self):
        src = read(MAIN)
        self.assertIn('"--selftest"', src)

    def test_result_file_is_the_verdict(self):
        """窗口版 exe 的 stdout 不可靠，判据必须是退出码 + 结果文件。"""
        src = read(MAIN)
        self.assertIn("IMGDEDUP_SELFTEST_OUT", src)

    def test_selftest_covers_the_risky_bits(self):
        src = read(MAIN)
        for token in ("CLIP 模型（外置）", "cv2 外置组件", "离线推理",
                      "底层库加载", "核心引擎导入", "界面模块导入",
                      "界面可实例化"):
            self.assertIn(token, src, f"自检缺少「{token}」这一项")

    def test_interact_test_flag_is_wired(self):
        """`--selftest` 证明不了"点得动、筛得对"（它只建窗口、行数为 0），
        所以另有一个 `--interact-test` 模式投递真实鼠标事件。
        它的模块是动态导入，静态分析看不到：必须进 hiddenimports，
        否则冻结后该模式直接 ImportError。"""
        main_src = read(MAIN)
        self.assertIn('"--interact-test"', main_src)
        self.assertIn("dev.click_frozen", main_src)

        spec_src = read(SPEC)
        self.assertIn('"dev.click_frozen"', spec_src,
                      "dev.click_frozen 没进 hiddenimports，冻结后会 ImportError")

    def test_utf8_is_forced(self):
        """冻结后控制台是 GBK，打印 ✅ 会 UnicodeEncodeError 并让自检带错退出。"""
        src = read(MAIN)
        self.assertIn("_force_utf8", src)
        self.assertIn("reconfigure", src)

    def test_run_selftest_is_callable(self):
        from imgdedup_ui.__main__ import run_selftest

        self.assertTrue(callable(run_selftest))


class TestGuiModeFlags(unittest.TestCase):
    """GUI 入口的附加模式。这些开关一旦断掉，CI 里看不出任何异常。"""

    def test_interact_test_wires_the_dev_module(self):
        """`--interact-test` 必须真的接到验证模块上，而不是个空壳分支。"""
        src = read(MAIN)
        idx = src.find('"--interact-test"')
        self.assertGreater(idx, -1)
        tail = src[idx:idx + 400]
        self.assertIn("interact_main", tail, "分支里没有调用验证入口")

    def test_verifier_exists_and_is_importable(self):
        mod = PROJ / "dev" / "click_frozen.py"
        self.assertTrue(mod.is_file(), "缺少 dev/click_frozen.py")
        src = read(mod)
        # 它必须自己处理 frozen/非 frozen 两种路径，否则冻结后 import 就炸
        self.assertIn('getattr(sys, "frozen", False)', src)
        # 判据必须是结果文件（窗口版 exe 的 stdout 不可靠）
        self.assertIn("IMGDEDUP_CLICK_OUT", src)

    def test_probe_script_flag_is_wired(self):
        """`--probe-script <file>` 允许在冻结产物里跑任意探针。

        为什么需要：`--selftest` / `--interact-test` 都是写死的，
        新增功能（预览缩放、动图、三档模式）没法验证"打包后还能用"。
        而"归档里有这个符号"只证明代码在包里，证明不了它跑得起来。

        这条测试守的是入口没被写死成某个固定脚本：
        必须用 `runpy.run_path` 跑传进来的路径。
        """
        src = read(MAIN)
        self.assertIn('"--probe-script"', src)
        idx = src.find('"--probe-script"')
        tail = src[idx:idx + 900]
        self.assertIn("runpy.run_path", tail,
                      "probe-script 分支没有真的执行传入的脚本")

    def test_probe_script_rejects_missing_argument(self):
        """不给路径时必须以非零码退出，不能静默成功。"""
        src = read(MAIN)
        idx = src.find('"--probe-script"')
        tail = src[idx:idx + 900]
        self.assertIn("return 2", tail, "缺参数时应返回非零码")


class TestGitignore(unittest.TestCase):
    def test_model_is_ignored(self):
        src = read(GITIGNORE)
        self.assertIn("resources/models/", src,
                      "85 MB 的模型不该进 git（要么忽略，要么走 LFS）")

    def test_build_junk_is_ignored(self):
        src = read(GITIGNORE)
        for token in ("build/", "dist/", ".scratch/", "__pycache__/"):
            self.assertIn(token, src, f".gitignore 缺少 {token}")

    def test_lfs_hint_is_documented(self):
        src = read(GITIGNORE)
        self.assertIn("git lfs track", src,
                      "想改用 LFS 的人应该能在 .gitignore 里找到命令")


# 期望的图标尺寸。改这里就必须同时改 dev/make_icon.py 的 ICON_SIZES。
EXPECTED_ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]


class TestIconAssets(unittest.TestCase):
    """图标是「派生产物」：源图改了要重生成，但忘了重生成不会让任何功能
    测试失败：只会让用户看到旧图标。所以把产物的形状钉死。
    """

    def test_icon_files_exist(self):
        self.assertTrue(ICON_ICO.is_file(), "缺少 resources/icon.ico")
        self.assertTrue(ICON_PNG.is_file(), "缺少 resources/icon.png")

    def test_ico_contains_every_size(self):
        """Windows 会按显示场景挑尺寸：任务栏 32、资源管理器大图标 256、
        桌面小图标 48。缺哪个都会看到模糊的缩放结果。"""
        from PIL import Image

        with Image.open(ICON_ICO) as im:
            got = sorted(s for s, _ in im.info.get("sizes", []))
        self.assertEqual(got, EXPECTED_ICON_SIZES,
                         "ICO 里的尺寸和期望不一致")

    def test_png_is_square_256(self):
        """运行时 setWindowIcon 用的是这张。非正方形在 Qt 里会被压扁。"""
        from PIL import Image

        with Image.open(ICON_PNG) as im:
            self.assertEqual(im.size, (256, 256), "运行时图标必须是 256×256")
            self.assertEqual(im.mode, "RGBA", "必须带 alpha，否则圆角变黑底")

    def test_icon_is_not_transparent_only(self):
        """防止「裁过头」：整张图裁完只剩透明像素，看起来像没图标。"""
        from PIL import Image

        with Image.open(ICON_PNG).convert("RGBA") as im:
            alpha = im.getchannel("A")
            lo, hi = alpha.getextrema()
            self.assertEqual(lo, 0, "应当保留透明背景")
            self.assertGreater(hi, 200, "不透明像素太少，图标可能是空的")

    def test_make_icon_sizes_match_test_expectation(self):
        """两个地方各写一份尺寸列表一定会漂移：这里把它们锁在一起。"""
        from dev.make_icon import ICON_SIZES

        self.assertEqual(sorted(ICON_SIZES), EXPECTED_ICON_SIZES)


class TestIconWiring(unittest.TestCase):
    """图标要「两处都做」且互不替代，只做一处用户就会看到两个不同的图标。"""

    def test_spec_embeds_icon_into_pe(self):
        """EXE(icon=...) 决定资源管理器里文件长什么样。

        断言必须写成带缩进的整行。第一版只断言 `"icon=str(ICON_ICO)"`
        这个子串：它是 `icon=str(ICON_ICO),` 的子串，把 spec 里那一行
        删掉后仍然命中（如果它出现在注释里）。这种"子串断言"是伪测试。
        """
        src = read(SPEC)
        self.assertIn("    icon=str(ICON_ICO),\n", src,
                      "spec 没把图标嵌进 PE，exe 文件本身会没图标")
        # 必须在 EXE(...) 调用内，不能只是模块级代码
        idx = src.find("    icon=str(ICON_ICO),\n")
        self.assertGreater(src.find("EXE("), -1)
        self.assertLess(src.find("EXE("), idx, "icon= 不在 EXE() 调用里")

    def test_spec_bundles_icon_as_data(self):
        """datas 里的图标决定运行时窗口图标，与 PE 那份互不替代。"""
        src = read(SPEC)
        self.assertIn('datas.append((str(ICON_ICO), "resources"))', src)
        self.assertIn('datas.append((str(ICON_PNG), "resources"))', src)

    def test_spec_fails_fast_when_icon_missing(self):
        src = read(SPEC)
        idx = src.find("ICON_ICO")
        self.assertGreater(idx, -1)
        self.assertIn("dev.make_icon", src,
                      "报错信息里要告诉用户怎么生成图标")

    def test_main_has_icon_helpers(self):
        """图标实现在 `imgdedup_ui/_entry_symbols.py`，不在 `__main__`。

        这是个结构性要求，不是风格偏好：PyInstaller 的入口脚本不进
        `PYZ.pyz`，放在 `__main__` 里的符号 `dev/check_frozen.py` 永远扫不到。
        所以这两个函数必须是「普通模块里定义 + 入口转发」。
        """
        # 真身在能被归档收录的普通模块里
        sym = PROJ / "imgdedup_ui" / "_entry_symbols.py"
        self.assertTrue(sym.is_file(), "缺少 imgdedup_ui/_entry_symbols.py")
        sym_src = read(sym)
        self.assertIn("def icon_path", sym_src)
        self.assertIn("def set_app_icon", sym_src)

        # 入口脚本转发，不能再有第二份实现（两份实现一定会漂移）
        main_src = read(MAIN)
        self.assertIn("from imgdedup_ui._entry_symbols import", main_src)
        self.assertIn("icon_path", main_src)
        self.assertIn("set_app_icon", main_src)
        self.assertNotIn("def icon_path", main_src,
                         "入口脚本里不该再有实现，只留转发")

    def test_main_sets_appusermodelid(self):
        """不设 AppUserModelID，Windows 会把进程归到 python.exe 那组，
        任务栏显示 Python 的图标。

        只断言名字不行：它在文档注释里也出现过一次，
        删掉真正的调用后测试仍然绿。必须锚定到带 `shell32.` 的调用行。
        """
        src = read(PROJ / "imgdedup_ui" / "_entry_symbols.py")
        self.assertIn("ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(",
                      src, "没有真正调用 SetCurrentProcessExplicitAppUserModelID")

    def test_icon_is_set_before_window_is_built(self):
        """图标必须在建主窗口之前设好，否则第一帧用的是默认羽毛图标。

        不能用 `src.find(...)` 取第一个命中：文件里有两个 `MainWindow(`
        （一个在条件分支里、一个在 main() 里），`set_app_icon(app)` 又排在
        定义区之后，于是第一个命中永远算"太晚"，测试恒红。
        → 必须在 `def main(` 之后的作用域里比较。
        """
        src = read(MAIN)
        scope = src[src.find("def main(argv=None)"):]
        self.assertGreater(len(scope), 0, "找不到 main()")
        call = scope.find("    set_app_icon(app)\n")
        ctor = scope.find("win = MainWindow()")
        self.assertGreater(call, -1, "main() 里没有调用 set_app_icon")
        self.assertGreater(ctor, -1, "main() 里没有建 MainWindow")
        self.assertLess(call, ctor, "set_app_icon 调用得太晚")

    def test_icon_path_covers_frozen_layouts(self):
        """冻结后图标可能在 _MEIPASS/resources、exe 同级 resources、
        或 onedir 的 _internal/resources：三个都得试。"""
        from imgdedup_ui.__main__ import icon_path

        import inspect

        src = inspect.getsource(icon_path)
        self.assertIn("_MEIPASS", src)
        self.assertIn("_internal", src)


class TestInstallerScript(unittest.TestCase):
    """安装包配置一旦退化，只有真去装一遍才发现：钉在源码层。"""

    def test_iss_exists(self):
        self.assertTrue(ISS.is_file(), "缺少 imgdedup.iss")

    def test_installs_per_user_without_uac(self):
        """本工具不需要任何系统级权限（数据在用户目录、不注册服务），
        所以装到 LocalAppData 全程不弹 UAC。"""
        src = read(ISS)
        self.assertIn("PrivilegesRequired=lowest", src)
        self.assertIn(r"{localappdata}\Programs", src)

    def test_creates_start_menu_and_desktop_icons(self):
        src = read(ISS)
        self.assertIn("[Icons]", src)
        self.assertIn("{group}", src, "缺开始菜单项")
        self.assertIn("{userdesktop}", src, "缺桌面快捷方式")
        self.assertIn("[Tasks]", src)
        self.assertIn("desktopicon", src)

    def test_uses_absolute_paths_via_defines(self):
        """相对路径在不同编译阶段 cwd 不一致：实测 `..\\dist\\installer`
        被解析成上一层目录，`..\\resources\\icon.ico` 直接报找不到。
        所以路径必须走编译期变量，由脚本传绝对路径。"""
        src = read(ISS)
        for var in ("{#SourceDir}", "{#IconFile}", "{#OutDir}"):
            self.assertIn(var, src, f"没有用编译期变量 {var}")
        self.assertIn("SetupIconFile={#IconFile}", src)
        # 不许再出现以 .. 开头的相对路径
        for line in src.splitlines():
            bare = line.strip()
            if bare.startswith(("SetupIconFile=", "OutputDir=", "Source:")):
                self.assertNotIn("..\\", bare,
                                 f"这是相对路径，会解析错：{bare}")

    def test_has_uninstaller(self):
        """能在「应用和功能」里卸载，靠的是 Inno 自带卸载器 + AppId。"""
        src = read(ISS)
        self.assertIn("AppId=", src)
        self.assertIn("[UninstallDelete]", src)

    def test_output_filename_carries_version(self):
        src = read(ISS)
        self.assertIn("OutputBaseFilename={#AppName}-{#AppVersion}-setup", src)


class TestInstallerScriptRunner(unittest.TestCase):
    def test_runner_exists(self):
        self.assertTrue(MAKE_INSTALLER.is_file(), "缺少 dev/make_installer.py")

    def test_version_comes_from_single_source(self):
        """版本号只能有一个来源（imgdedup_core.__version__）。
        两处各写一份，早晚有一天对不上。

        直接比较 `version()` 与 `__version__` 抓不到漂移，因为
        `version()` 本来就是读 `__version__` 的，改包版本号两边一起变。
        真正要钉的是：iss 里的版本号必须来自 /D 编译期变量，
        而不是写死的字面量（写死才会有一天和包版本对不上）。
        """
        from dev.make_installer import version
        from imgdedup_core import __version__

        self.assertEqual(version(), __version__)

        # iss 必须用编译期变量，不能写死
        iss = read(ISS)
        self.assertIn("OutputBaseFilename={#AppName}-{#AppVersion}-setup", iss)
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+", "版本号格式变了")

        # make_installer 必须真的把版本号作为 /D 传下去
        runner = read(MAKE_INSTALLER)
        self.assertIn('f"/DAppVersion={ver}"', runner,
                      "没有把版本号传给 ISCC，iss 会用到默认值")

    def test_iscc_lookup_covers_winget_location(self):
        """winget 把 Inno 装在 %LOCALAPPDATA%\\Programs，不在 Program Files。"""
        src = read(MAKE_INSTALLER)
        self.assertIn("ISCC_CANDIDATES", src)
        self.assertIn("Inno Setup 6", src)

    def test_build_exit_code_follows_the_check(self):
        """体检 FAIL 时 `--build` 必须退非 0。

        早先 `main()` 把 `check()` 的返回值丢掉了，永远 `return 0` ——
        release.yml 那一步照样绿，坏掉的安装包会被直接发到 Release 页。
        """
        from unittest import mock

        from dev import make_installer as M

        with mock.patch.object(M, "build_onedir"), \
                mock.patch.object(M, "build_installer"), \
                mock.patch.object(M, "check", return_value=1):
            self.assertEqual(M.main(["--build"]), 1,
                             "体检 FAIL 却退 0 —— release 会把坏包发出去")
        with mock.patch.object(M, "build_onedir"), \
                mock.patch.object(M, "build_installer"), \
                mock.patch.object(M, "check", return_value=0):
            self.assertEqual(M.main(["--build"]), 0)

    def test_portable_only_does_not_touch_the_installer(self):
        """`--portable` 只打免安装版：不许调 ISCC。

        这条路是给"没装 Inno Setup 也要验证免安装版"用的 ——
        以前只能跑 `--build`，然后卡在 ISCC 找不到。
        """
        from unittest import mock

        from dev import make_installer as M

        with mock.patch.object(M, "build_onedir") as bo, \
                mock.patch.object(M, "build_installer") as bi, \
                mock.patch.object(M, "check", return_value=0) as ck:
            self.assertEqual(M.main(["--portable"]), 0)
        bo.assert_called_once()
        bi.assert_not_called()
        ck.assert_called_once_with(portable_only=True)

    def test_portable_and_build_are_mutually_exclusive(self):
        """两个一起给要直接报错，而不是猜哪个优先。"""
        from dev import make_installer as M

        with self.assertRaises(SystemExit):
            M.main(["--portable", "--build"])

    def test_portable_only_check_ignores_missing_installer(self):
        """免安装版在位、安装包缺席：`--portable` 的体检要 PASS。

        对照：不带 `portable_only` 时同样这一份产物必须 FAIL ——
        否则"两种形态都必须在"这条判据就被悄悄放松了。
        """
        import pathlib as _pathlib
        import tempfile
        from unittest import mock

        from dev import make_installer as M

        with tempfile.TemporaryDirectory() as td:
            td = _pathlib.Path(td)
            one = td / "ImgDedup"
            one.mkdir()
            (one / "ImgDedup.exe").write_bytes(b"x")
            (one / "extra").mkdir()

            # PROJECT_ROOT 也得一起换：check() 用 relative_to(PROJECT_ROOT)
            # 打相对路径，产物在临时目录里时会抛 ValueError。
            # （真实用法下产物一定在项目里，所以这不是脚本的缺陷。）
            with mock.patch.object(M, "PROJECT_ROOT", td), \
                    mock.patch.object(M, "ONEDIR", one), \
                    mock.patch.object(M, "INSTALLER_DIR", td / "installer"), \
                    mock.patch.object(M, "version", return_value="0.0.0"), \
                    mock.patch.object(M, "read_exe_icon_id",
                                      return_value="图标组 1 个"):
                self.assertEqual(
                    M.check(portable_only=True), 0,
                    "安装包缺席不该让 --portable 的体检失败")
                self.assertEqual(
                    M.check(), 1,
                    "不带 portable_only 时安装包缺席必须判 FAIL")

    def test_reads_pe_icon_via_windows_api(self):
        """自研 PE 解析出过假阴性（明明嵌了图标却报 None）。
        必须走 LoadLibraryExW + EnumResourceNamesW。

        别只断言这两个名字：注释里提一句就骗过了。锚定到
        `ctypes` 调用形式，并要求 `LOAD_LIBRARY_AS_DATAFILE` 这个标志也在
        （它决定"只映射不执行"，对未验证的 exe 才是安全的）。
        """
        src = read(MAKE_INSTALLER)
        self.assertIn("LoadLibraryExW(", src)
        self.assertIn("LOAD_LIBRARY_AS_DATAFILE", src)
        self.assertIn("EnumResourceNamesW(", src)
        self.assertIn("RT_GROUP_ICON", src)
        # ctypes 句柄必须用 c_void_p，用 wintypes.HMODULE 会 OverflowError
        self.assertIn("c_void_p", src,
                      "64 位 HMODULE 必须用 c_void_p，否则 OverflowError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
