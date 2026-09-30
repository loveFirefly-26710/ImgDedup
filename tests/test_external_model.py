"""外置组件 CLIP 模型：路径约定、落盘位置、缺模型时的档位拦截。

模型和 cv2 一样不随包分发（85 MB），用户单独放置 / 程序内自动下载，
位置是 `<exe 同级>/extra/models/`。

这块最容易"悄悄退化"的地方：
  · 有人把模型塞回 `datas` → 安装包体积立刻反弹 85 MB（`test_packaging`
    从反方向钉了这条）；
  · 路径约定被改到别处 → 用户按文档放好了，程序却找不到；
  · 缺模型时拦住了程序启动 → 快速/精确两档明明不需要它，
    却让整个软件起不来（所以它是"用到才问"，入口在 `_guard_deep_mode`）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJ))

CORE = PROJ / "imgdedup_core"
MAIN = PROJ / "imgdedup_ui" / "__main__.py"
SYMS = PROJ / "imgdedup_ui" / "_entry_symbols.py"
WINDOW = PROJ / "imgdedup_ui" / "window.py"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestPathConvention(unittest.TestCase):
    """路径约定：`<exe 同级>/extra/models/`，与 cv2 共用同一个 `extra/`。"""

    def test_extra_dirname_is_shared_with_cv2(self):
        """两边的 `extra` 目录名必须是同一个常量，不能各写一份。

        各写一份的话，改了其中一处就会出现"cv2 在 extra、模型在 extras"
        这种谁也想不到的错位。
        """
        from imgdedup_core import embed as EMB
        from imgdedup_core import opencv as OPENCV

        self.assertEqual(EMB.EXTRA_DIRNAME, "extra")
        self.assertIs(EMB.EXTRA_DIRNAME, OPENCV.EXTRA_DIRNAME)

    def test_model_extra_dir_is_extra_models(self):
        from imgdedup_core import embed as EMB

        self.assertEqual(EMB.model_extra_dir(),
                         EMB.extra_dir() / EMB.BUNDLE_SUBDIR)
        self.assertEqual(EMB.model_extra_dir().name, "models")

    def test_extra_models_is_the_first_candidate(self):
        """用户"自己放好的"必须优先于其它候选位置。

        否则会出现"文件明明放进去了，程序却用了别处的副本"这种
        看不见的错位：用户会以为自己的操作没生效。
        """
        src = read(CORE / "embed.py")
        i_extra = src.index("exe_dir / EXTRA_DIRNAME / BUNDLE_SUBDIR")
        i_meipass = src.index("Path(meipass) / BUNDLE_SUBDIR")
        self.assertLess(i_extra, i_meipass,
                        "extra/models 不是第一个候选位置")


class TestEnsureModelLandsWhereDocumented(unittest.TestCase):
    """`ensure_model(dest=...)` 必须把模型落到调用方指定的目录。

    提示文案里写的是 `extra/models`，下载就得落到那儿 ——
    落到别处会让用户"下完了却找不到"。
    """

    def test_returns_dest_when_already_complete(self):
        """`dest` 是模型根目录，返回的是装着文件的目录（含 repo slug）。

        这两个概念差一层目录，搞混的后果是"下载成功却永远读不到"：
        文件落到 `extra/models/vision_model.onnx`，而读取路径找的是
        `extra/models/<slug>/vision_model.onnx`。
        """
        from imgdedup_core import embed as EMB

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "models"
            d = root / EMB.repo_slug()
            d.mkdir(parents=True)
            for name in EMB.MODEL_FILES.values():
                (d / name).write_bytes(b"x")
            got = EMB.ensure_model(dest=root)
            self.assertEqual(got, d, "返回的不是装着模型文件的目录")
            self.assertEqual(got, root / EMB.repo_slug())

    def test_download_file_reports_progress(self):
        """进度回调必须真的被调用，否则下载对话框永远停在 0%。

        用 `file://` URL 离线验证，不联网。
        """
        from imgdedup_core import embed as EMB

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.bin"
            src.write_bytes(b"a" * (3 << 20))       # 3 MB，越过 2MB 节流阈值
            seen: list = []
            out = Path(td) / "out.bin"
            EMB._download_file(src.as_uri(), out, quiet=True,
                               cb=lambda g, t, m: seen.append((g, t, m)))

            self.assertTrue(seen, "进度回调一次都没被调用")
            self.assertEqual(seen[-1][0], 3 << 20, "最后一次回调的字节数不对")
            self.assertEqual(out.stat().st_size, 3 << 20, "文件没下完整")


class TestDeepModeGuard(unittest.TestCase):
    """「深度」缺模型时当场提示并把档位拨回去；不拦程序启动。

    拦截挂在 `activated`（只有用户操作才触发），不挂
    `currentIndexChanged`（程序化 `setCurrentIndex` 也会触发）。
    挂错的后果实测过：全新克隆里跑测试，整套测试就挂在一条"把档位设成
    深度"的用例上，因为弹出了模态对话框 `exec()`，没人点它。
    CI 上就是挂到 job 超时。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _win(self):
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        self.addCleanup(w.close)
        return w

    def test_guard_is_wired_to_activated(self):
        """这条守的是"别把拦截挂回 `currentIndexChanged`"。

        挂错 = 程序化设置档位时弹模态框 = 测试 / CI 卡死。
        """
        src = read(WINDOW)
        self.assertIn(
            "self.cmb_mode.activated.connect(self._guard_deep_mode)", src,
            "拦截没挂在 activated 上")
        self.assertNotIn(
            "currentIndexChanged.connect(self._guard_deep_mode)", src,
            "拦截挂回 currentIndexChanged 了 —— 程序化设置会弹模态框卡死")

    def test_programmatic_change_never_prompts(self):
        """程序化把档位设成「深度」绝不能弹对话框。

        这是上面那条卡死事故的直接回归测试：用 `setCurrentIndex`
        （不是 `activated`）切档时，拦截函数一次都不该被调用。
        """
        from unittest import mock

        from imgdedup_core import embed as EMB
        from imgdedup_ui import _entry_symbols as S

        win = self._win()
        called: list = []

        def _fake_ready(*a, **k):
            called.append(1)
            return False

        with mock.patch.object(EMB, "is_ready", return_value=False), \
                mock.patch.object(S, "ensure_model_ready", _fake_ready):
            win.cmb_mode.setCurrentIndex(2)          # 程序化，不是用户操作

        self.assertEqual(
            called, [],
            "程序化设置档位也走了拦截 —— 会弹模态框把测试 / CI 卡死")

    def test_user_picking_deep_without_model_reverts(self):
        """用户真的选了「深度」但拿不到模型 → 拨回上一次选中的那一档。"""
        from unittest import mock

        from imgdedup_core import embed as EMB
        from imgdedup_ui import _entry_symbols as S

        win = self._win()
        win.cmb_mode.setCurrentIndex(1)              # 先停在「精确」
        win.cmb_mode.activated.emit(1)               # 模拟用户选「精确」

        with mock.patch.object(EMB, "is_ready", return_value=False), \
                mock.patch.object(S, "ensure_model_ready", return_value=False):
            win.cmb_mode.setCurrentIndex(2)          # 用户选「深度」
            win.cmb_mode.activated.emit(2)           # ← 只有这个才代表用户操作

        self.assertEqual(win.cmb_mode.currentIndex(), 1,
                         "缺模型时没把档位拨回上一档 —— 用户会以为深度模式生效了")

    def test_user_picking_deep_with_model_stays(self):
        from unittest import mock

        from imgdedup_core import embed as EMB

        win = self._win()
        win.cmb_mode.setCurrentIndex(1)
        win.cmb_mode.activated.emit(1)
        with mock.patch.object(EMB, "is_ready", return_value=True):
            win.cmb_mode.setCurrentIndex(2)
            win.cmb_mode.activated.emit(2)
        self.assertEqual(win.cmb_mode.currentIndex(), 2,
                         "模型在位却还是被拨回去了")

    def test_switching_to_other_modes_is_never_blocked(self):
        from unittest import mock

        from imgdedup_core import embed as EMB
        from imgdedup_ui import _entry_symbols as S

        win = self._win()
        with mock.patch.object(EMB, "is_ready", return_value=False), \
                mock.patch.object(S, "ensure_model_ready", return_value=False):
            for i in (1, 0, 1):
                win.cmb_mode.setCurrentIndex(i)
                win.cmb_mode.activated.emit(i)
                self.assertEqual(win.cmb_mode.currentIndex(), i)

    def test_model_is_not_required_at_startup(self):
        """启动期不许有"缺模型就退出"的检查。

        模型只服务「深度」一档；为一个可选档位拦住整个程序启动，
        用户会以为软件坏了。
        """
        src = read(MAIN)
        self.assertNotIn("_require_model", src,
                         "启动期不该检查模型 —— 快速/精确两档不需要它")


class TestDownloadWiring(unittest.TestCase):
    def test_symbols_live_in_importable_module(self):
        """必须放在 `_entry_symbols.py`，不能在 `__main__.py`。

        入口脚本冻结后不可导入（不在 PYZ 模块表里），探针进不去，
        这条链路就没法在产物里验证，而它恰恰是最容易出问题的地方
        （进度对话框 + 后台线程 + 事件循环）。
        """
        src = read(SYMS)
        for token in ("class _ModelDownloadTask", "def _download_model",
                      "def ensure_model_ready"):
            self.assertIn(token, src, f"_entry_symbols.py 缺少 {token}")

    def test_was_canceled_is_read_before_close(self):
        """`QProgressDialog.close()` 本身就会发 `canceled`。

        顺序反了，"下载成功"会被判成"用户取消"，永远返回失败。
        锚点用整条语句：裸名字 `wasCanceled()` 会命中注释，
        子串断言就变成恒真了（这个坑本项目踩过）。
        """
        src = read(SYMS)
        body = src[src.index("def _download_model"):
                   src.index("def ensure_model_ready")]
        i_read = body.index("user_canceled = box.wasCanceled()")
        i_close = body.index("box.close()")
        self.assertLess(i_read, i_close,
                        "wasCanceled() 必须排在 close() 之前")

    def test_verify_after_download_not_trusting_the_flag(self):
        """下载"报成功"不等于装好了：必须再查一次实际状态。"""
        src = read(SYMS)
        body = src[src.index("def ensure_model_ready"):]
        self.assertIn("EMB.is_ready()", body,
                      "下载后没有再查一次实际状态")


class TestLoadNeverDownloads(unittest.TestCase):
    """`ClipEncoder.load()` 不许联网。

    下载只能由 UI 显式发起（`ensure_model_ready` → 带进度对话框）。
    让后台扫描线程悄悄下 85 MB 是灾难级体验：用户看不到进度，只觉得卡住。
    而测试 / CI 上会变成"跑一遍测试顺便下模型"：实测模拟全新克隆
    （没有模型）时，整个测试套件卡在网络上十几分钟。
    """

    def test_load_does_not_call_ensure_model(self):
        from unittest import mock

        from imgdedup_core import embed as EMB

        called: list = []
        with mock.patch.object(EMB, "is_ready", return_value=False), \
                mock.patch.object(EMB, "ensure_model",
                                  side_effect=lambda **kw: called.append(kw)):
            enc = EMB.ClipEncoder()
            ok = enc.load()

        self.assertFalse(ok, "缺模型时 load() 应返回 False")
        self.assertEqual(
            called, [],
            "load() 联网下载模型了 —— 下载必须由 UI 显式发起，"
            "不能在后台线程里静默下 85 MB")


class TestSelftestToleratesMissingModel(unittest.TestCase):
    def test_selftest_skips_inference_without_model(self):
        """模型缺失时「离线推理」要跳过而不是判失败。

        否则"按设计没装可选组件"会被报成"包坏了"，把真正的失败淹掉。
        """
        src = read(MAIN)
        self.assertIn("EMB.is_ready()", src)
        self.assertIn("跳过：CLIP 模型未安装", src)


if __name__ == "__main__":
    unittest.main()
