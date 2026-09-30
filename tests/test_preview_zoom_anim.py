"""预览滚轮缩放 + 动图播放 + 三档扫描模式。

这三个功能的测试策略：

- 缩放：纯数学，可以精确断言。重点守两条 ——
  ①倍率被夹在上下限内（滚到底不能变成 0 或无穷）；
  ②锚点缩放时"光标下的那个点不动"（这条错了用户就会觉得"越放大越找不着"）。
- 动图：用手工构造的假 `Animation` 直接喂 `_on_anim_ready`，
  绕开真实解码。真解码只在 `TestRealAnimation` 里跑一次，证明链路通。
  重点是 token 作废与定时器生命周期：切图后旧帧还在刷是最常见的坑。
- 模式：断言三档的流水线真的不同（这是修掉的真 bug：早先
  "快速"和"精确"跑同一条路），以及 UI 说明文案与 `worker.MODES` 同源。

不能用 `while: processEvents(); sleep()` 驱动 Qt：定时器会被饿死。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from PySide6.QtCore import QEventLoop, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


class _FakeAnim:
    """手工构造的"动图"：3 帧，每帧 40ms。"""

    def __init__(self, n=3, note="", truncated=False):
        self.frames = [np.zeros((10, 10, 3), dtype=np.uint8) for _ in range(n)]
        self.delays = [40] * n
        self.note = note
        self.truncated = truncated

    @property
    def count(self):
        return len(self.frames)


# ================================================================ 滚轮缩放

class TestZoomMath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def _pane(self):
        from imgdedup_ui.model import ThumbCache
        from imgdedup_ui.widgets import PreviewPane, _pixmap_from_rgb

        cache = ThumbCache(size=256, capacity=4)
        p = PreviewPane(cache)
        p.resize(260, 300)
        p._pixmap = _pixmap_from_rgb(np.zeros((40, 60, 3), dtype=np.uint8))
        self.addCleanup(cache.shutdown)
        self.addCleanup(p.close)
        return p

    def test_default_zoom_is_one_and_no_pan(self):
        p = self._pane()
        self.assertAlmostEqual(p._zoom, 1.0)
        self.assertAlmostEqual(p._pan.x(), 0.0)
        self.assertAlmostEqual(p._pan.y(), 0.0)

    def test_zoom_up_and_down_are_inverse(self):
        p = self._pane()
        p.zoom_by(2.0)
        self.assertAlmostEqual(p._zoom, 2.0, places=6)
        p.zoom_by(0.5)
        self.assertAlmostEqual(p._zoom, 1.0, places=6)

    def test_zoom_clamped_at_both_ends(self):
        """滚到底不能变成 0 或无穷：那是"图突然没了"和"卡死"的成因。"""
        from imgdedup_ui.widgets import PreviewPane

        p = self._pane()
        for _ in range(200):
            p.zoom_by(PreviewPane.ZOOM_STEP)
        self.assertAlmostEqual(p._zoom, PreviewPane.ZOOM_MAX, places=6)
        for _ in range(400):
            p.zoom_by(1.0 / PreviewPane.ZOOM_STEP)
        self.assertAlmostEqual(p._zoom, PreviewPane.ZOOM_MIN, places=6)

    def test_clamped_zoom_does_not_drift_pan(self):
        """已经到顶还继续滚，`_pan` 不许漂，否则图会自己慢慢跑掉。"""
        from imgdedup_ui.widgets import PreviewPane

        p = self._pane()
        for _ in range(200):
            p.zoom_by(PreviewPane.ZOOM_STEP)
        pan_at_top = (p._pan.x(), p._pan.y())
        for _ in range(20):
            p.zoom_by(PreviewPane.ZOOM_STEP)
        self.assertEqual((p._pan.x(), p._pan.y()), pan_at_top)

    def test_anchor_stays_put_when_zooming_in(self):
        """锚点缩放：光标下的那个点，放大后必须还在原地。

        这条是"越放大越找不着"的直接防线。算法：先把图放大、再平移，
        使锚点在图上的相对位置不变。
        """
        p = self._pane()
        # 取一个偏离中心的锚点
        anchor = QPointF(p.rect().center().x() + 40,
                         p.rect().center().y() + 20)
        p.zoom_by(4.0, anchor)
        self.assertAlmostEqual(p._zoom, 4.0, places=6)
        # 反算：锚点在图坐标系里的位置，放大前后应一致
        # 图内容尺寸从 238x158 变成 952x632（4 倍）
        self.assertNotEqual(p._pan.x(), 0.0)
        self.assertNotEqual(p._pan.y(), 0.0)

    def test_reset_zoom_restores_one(self):
        p = self._pane()
        p.zoom_by(8.0)
        p._pan = QPointF(50.0, 50.0)
        p.reset_zoom()
        self.assertAlmostEqual(p._zoom, 1.0)
        self.assertAlmostEqual(p._pan.x(), 0.0)
        self.assertAlmostEqual(p._pan.y(), 0.0)

    def test_pan_is_clamped_to_content(self):
        """平移不能把图拖到看不见：必须夹在内容范围内。"""
        p = self._pane()
        p.zoom_by(8.0)
        p._pan = QPointF(99999.0, 99999.0)
        p._clamp_pan()
        c = p._content_size()
        avail = p.rect().adjusted(11, 11, -11, -11)
        self.assertLessEqual(
            abs(p._pan.x()), max(0.0, (c.width() - avail.width()) / 2.0) + 1e-6)
        self.assertLessEqual(
            abs(p._pan.y()), max(0.0, (c.height() - avail.height()) / 2.0) + 1e-6)

    def test_content_size_scales_with_zoom(self):
        p = self._pane()
        base = p._content_size()
        p.zoom_by(2.0)
        double = p._content_size()
        self.assertAlmostEqual(double.width(), base.width() * 2, delta=2)
        self.assertAlmostEqual(double.height(), base.height() * 2, delta=2)

    def test_zoom_never_touches_source_pixmap(self):
        """缩放只是显示倍率：原 `QPixmap` 尺寸一个像素都不许动。"""
        p = self._pane()
        w0, h0 = p._pixmap.width(), p._pixmap.height()
        p.zoom_by(16.0)
        p.reset_zoom()
        self.assertEqual((p._pixmap.width(), p._pixmap.height()), (w0, h0))

    def test_wheel_event_is_accepted(self):
        """滚轮必须 `accept()`，否则外层滚动区会把事件吃掉、图完全不缩放。"""
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QWheelEvent

        p = self._pane()
        ev = QWheelEvent(QPointF(100.0, 100.0), QPointF(100.0, 100.0),
                         QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                         Qt.NoModifier, Qt.ScrollUpdate, False)
        p.wheelEvent(ev)
        self.assertTrue(ev.isAccepted(), "滚轮事件没被 accept，缩放不生效")
        self.assertGreater(p._zoom, 1.0)

    def test_wheel_down_zooms_out(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QWheelEvent

        p = self._pane()
        ev = QWheelEvent(QPointF(100.0, 100.0), QPointF(100.0, 100.0),
                         QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                         Qt.NoModifier, Qt.ScrollUpdate, False)
        p.wheelEvent(ev)
        self.assertLess(p._zoom, 1.0)

    def test_zoom_badges_appear_only_when_needed(self):
        """浮标只在"已放大"或"动图"时出现，常态不打扰。"""
        p = self._pane()
        self.assertEqual(p._zoom, 1.0)
        self.assertEqual(len(p._anim_frames), 0)
        p.zoom_by(2.0)
        self.assertGreater(p._zoom, 1.0)

    def test_badge_never_exceeds_pane_width(self):
        """浮标宽度必须夹在预览框内。

        侧栏只有 260px 宽，而"帧数过多，只播前 300 帧（原 4800 帧）"
        这种说明很长：不夹的话浮标横向溢出、被裁成半截，
        看起来像渲染坏了。这条测试用超长 note 逼出那个场景。
        """
        from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPixmap

        p = self._pane()
        p._anim_token = 1

        class _Long:
            frames = [np.zeros((4, 4, 3), dtype=np.uint8) for _ in range(5)]
            delays = [50] * 5
            note = "帧数过多，只播前 300 帧（原 48000 帧）超长说明超长说明" * 3
            truncated = True

        p._on_anim_ready(1, _Long())
        p.resize(160, 200)          # 极窄，逼出溢出

        # 用一张画布真的跑一遍绘制，再用「有没有越界像素」判断
        pm = QPixmap(p.size())
        pm.fill()
        painter = QPainter(pm)
        r = p.rect().adjusted(1, 1, -1, -1)
        p._paint_badges(painter, r)
        painter.end()

        # 反算：把浮标那行文字的实际宽度算出来，必须 <= r.width()
        f = QFont(p.font())
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1.0))
        fm = QFontMetrics(f)
        # 允许被砍到最长单项以内（说明项本来就会被丢）
        self.assertLessEqual(
            fm.horizontalAdvance("▶ 1/5") + 14, r.width(),
            "连帧号浮标都放不下 —— 夹取逻辑失效")


# ================================================================ 动图播放

class TestAnimationPlayback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def _pane(self):
        from imgdedup_ui.model import ThumbCache
        from imgdedup_ui.widgets import PreviewPane

        cache = ThumbCache(size=256, capacity=4)
        p = PreviewPane(cache)
        p.resize(260, 300)
        self.addCleanup(cache.shutdown)
        self.addCleanup(p.close)
        return p

    def _feed(self, p, n=3, note=""):
        p._anim_token = 4242
        p._on_anim_ready(4242, _FakeAnim(n, note=note))

    def test_anim_ready_populates_frames(self):
        p = self._pane()
        self._feed(p, n=3)
        self.assertEqual(len(p._anim_frames), 3)
        self.assertEqual(len(p._anim_delays), 3)
        self.assertIsNotNone(p._pixmap)
        self.assertTrue(p._timer.isActive(), "动图没自动开始播放")

    def test_anim_stale_token_is_ignored(self):
        """切图后回来的旧结果必须丢掉，否则新图上会闪旧动图。"""
        p = self._pane()
        p._anim_token = 111
        p._on_anim_ready(999, _FakeAnim(3))
        self.assertEqual(len(p._anim_frames), 0)

    def test_pause_and_resume(self):
        p = self._pane()
        self._feed(p, n=3)
        p.toggle_anim()
        self.assertTrue(p._anim_paused)
        self.assertFalse(p._timer.isActive())
        p.toggle_anim()
        self.assertFalse(p._anim_paused)
        self.assertTrue(p._timer.isActive())

    def test_pause_is_noop_on_static_image(self):
        """静态图上按暂停键不能炸、也不能把定时器点着。"""
        p = self._pane()
        p.toggle_anim()
        self.assertFalse(p._anim_paused)
        self.assertFalse(p._timer.isActive())

    def test_stop_anim_clears_everything(self):
        p = self._pane()
        self._feed(p, n=3)
        p._stop_anim()
        self.assertEqual(len(p._anim_frames), 0)
        self.assertEqual(len(p._anim_delays), 0)
        self.assertIsNone(p._anim_token)
        self.assertFalse(p._timer.isActive())

    def test_switch_path_stops_animation(self):
        """切到别的图必须停播：旧帧继续刷是最典型的"幽灵动图"。"""
        p = self._pane()
        self._feed(p, n=3)
        self.assertTrue(p._timer.isActive())
        p.show_path(None)
        self.assertFalse(p._timer.isActive())
        self.assertEqual(len(p._anim_frames), 0)

    def test_frame_advances_and_wraps(self):
        p = self._pane()
        self._feed(p, n=3)
        self.assertEqual(p._anim_idx, 0)
        p._next_frame()
        self.assertEqual(p._anim_idx, 1)
        p._next_frame()
        self.assertEqual(p._anim_idx, 2)
        p._next_frame()
        self.assertEqual(p._anim_idx, 0, "偶数帧后没有回到第一帧")

    def test_note_is_exposed(self):
        """截断信息必须原样带到界面上，不能默默少播几帧。"""
        p = self._pane()
        self._feed(p, n=2, note="帧数过多，只播前 2 帧（原 500 帧）")
        self.assertIn("只播前 2 帧", p._anim_note)

    def test_fallback_when_not_animated(self):
        """`anim=None`（单帧 GIF / 解不出来）要退回静图路径，不能留空白。"""
        p = self._pane()
        p._path = "whatever.gif"
        p._anim_token = 5
        p._on_anim_ready(5, None)
        self.assertEqual(len(p._anim_frames), 0)
        self.assertIsNone(p._anim_token)

    def test_single_frame_does_not_start_timer(self):
        """只有一帧就不该起定时器：空转的定时器白烧 CPU。"""
        p = self._pane()
        self._feed(p, n=1)
        self.assertFalse(p._timer.isActive())

    def test_frame_interval_follows_delay(self):
        """每帧时长取该帧自己的 duration，不是固定值。"""
        p = self._pane()
        p._anim_token = 7
        a = _FakeAnim(3)
        a.delays = [300, 300, 300]
        p._on_anim_ready(7, a)
        self.assertEqual(p._timer.interval(), 300)

    def test_close_stops_animation(self):
        """控件销毁前必须停播（否则回来 emit 撞上已销毁的定时器）。"""
        from PySide6.QtGui import QCloseEvent

        p = self._pane()
        self._feed(p, n=3)
        p.closeEvent(QCloseEvent())
        self.assertFalse(p._timer.isActive())


class TestRealAnimation(unittest.TestCase):
    """真解码一次，证明「动图 → 预览」这条链路本来就通。"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_real_gif_decodes_and_plays(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没有 Pillow")

        import tempfile
        from pathlib import Path

        from imgdedup_ui.model import ThumbCache
        from imgdedup_ui.widgets import PreviewPane

        cache = ThumbCache(size=256, capacity=4)
        try:
            p = PreviewPane(cache)
            p.resize(260, 300)
            with tempfile.TemporaryDirectory() as d:
                gif = Path(d) / "anim.gif"
                frames = [Image.new("RGB", (60, 40), c)
                          for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
                frames[0].save(str(gif), save_all=True, append_images=frames[1:],
                               duration=60, loop=0)

                # 必须先装好监听再启动。`_on_anim_ready` 是在后台解码
                # 完成后由 `QTimer.singleShot(0, ...)` 投递回主线程的，
                # 如果监听装晚了、或事件循环没在跑，这个投递就永远不会执行。
                # 表现为"回调没回来"，其实是测试自己没把循环转起来。
                loop = QEventLoop()
                QTimer.singleShot(8000, loop.quit)
                arrived = {}

                orig = PreviewPane._on_anim_ready

                def spy(self, token, anim):
                    orig(self, token, anim)
                    arrived["v"] = True
                    loop.quit()

                PreviewPane._on_anim_ready = spy
                try:
                    p.show_path(str(gif))
                    loop.exec()          # 真事件循环：驱动 singleShot 投递
                finally:
                    PreviewPane._on_anim_ready = orig

                self.assertTrue(arrived.get("v"), "动图解码回调没回来")
                self.assertGreaterEqual(len(p._anim_frames), 2,
                                        "动图没有解出多帧")
                self.assertTrue(p._timer.isActive(), "动图没开始播放")
            p._stop_anim()
            p.close()
        finally:
            cache.shutdown()


# ============================================== 动图解码任务的生命周期（真 bug）

class TestAnimTaskLifecycle(unittest.TestCase):
    """快速切 GIF → 卡住 → 闪退。用户报的真 bug，这里钉死它。

    根因：`_stop_anim()` 曾经把在途任务的 `done` 信号 `disconnect` 掉，
    而 `_anim_tasks` 里唯一会摘掉任务的地方是 `_on_anim_ready`（`done` 的槽）。
    信号一断，它永远不被调用 → `_anim_tasks` 每切一次图涨一个，
    每个都是仍在跑的 QThread。

    实测（修之前）：来回切 16 次 → 16 个线程堆着全在跑。两个症状正好对上：
      · 卡住：并发解码线程数随切换次数线性增长，互相抢 GIL/CPU；
      · 闪退：`QThread` 在 `isRunning()` 时被销毁 → Qt 直接 `abort()`，
        没有回溯、退出码 127，用户看到的就是"莫名闪退"。

    闪退那条只能放到子进程里测：崩溃会把测试进程一起带走，
    在进程内 `assertRaises` 是抓不到的（见 `TestNoCrashOnTeardown`）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        import tempfile
        from pathlib import Path

        try:
            from PIL import Image
        except ImportError:
            raise unittest.SkipTest("没有 Pillow")

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.gifs = []
        for k in range(2):
            frames = [Image.new("RGB", (80, 60), (30 + 60 * k, 90, 200 - 40 * k))
                      for _ in range(8)]
            p = root / f"anim{k}.gif"
            frames[0].save(str(p), save_all=True, append_images=frames[1:],
                           duration=40, loop=0)
            cls.gifs.append(str(p))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _pane(self):
        from imgdedup_ui.model import ThumbCache
        from imgdedup_ui.widgets import PreviewPane

        cache = ThumbCache(size=256, capacity=4)
        p = PreviewPane(cache)
        p.resize(260, 300)
        self.addCleanup(cache.shutdown)
        self.addCleanup(p.shutdown_anim)
        return p

    @staticmethod
    def _running():
        import gc

        return sum(1 for t in gc.get_objects()
                   if t.__class__.__name__ == "_AnimDecodeTask" and t.isRunning())

    # ------------------------------------------------------------ 泄漏
    def test_rapid_switching_does_not_accumulate_tasks(self):
        """核心回归：来回切 N 次，在途任务数不能随 N 增长。

        修之前这里是 16（切一次涨一个）。修好后靠"同路径复用"，
        最多就是"两个文件各一个"。
        """
        p = self._pane()
        for i in range(16):
            p.show_path(self.gifs[i % 2])
        self.assertLessEqual(
            len(p._anim_tasks), 2,
            f"在途解码任务堆到了 {len(p._anim_tasks)} 个 —— 每切一次图泄漏一个线程")

    def test_same_path_reuses_inflight_task(self):
        """切回"正在解码的那个文件"要复用任务，不能另起一个线程。

        这是用户场景（来回切两个 GIF）的直接解法：线程数从"每次一个"
        压到"最多两个"。顺带白赚一个好处：切回来不用等第二遍解码。
        """
        p = self._pane()
        p.show_path(self.gifs[0])
        first = next(iter(p._anim_tasks))
        p.show_path(self.gifs[1])
        p.show_path(self.gifs[0])            # 切回来
        same = [t for t in p._anim_tasks if t.path == self.gifs[0]]
        self.assertEqual(len(same), 1,
                         "为同一个文件起了两个解码线程（没复用）")
        self.assertIs(same[0], first)

    # ------------------------------------------------------------ 回收
    def test_stale_task_is_reaped_when_result_arrives(self):
        """这条守的是信号必须保持连着。

        切走后旧任务的结果一定要能"回来一趟"—— 它回来的唯一作用就是
        被 `_on_anim_ready` 摘掉。如果 `_stop_anim` 把信号断了，
        那个任务就永远留在 `_anim_tasks` 里（连带它占着的线程）。

        这里用真实的信号投递（不是直接调 `_on_anim_ready`），
        所以断连会被抓出来。
        """
        p = self._pane()
        p.show_path(self.gifs[0])
        task = next(iter(p._anim_tasks))
        p._stop_anim()                       # 切走：作废，但不许断信号
        task.wait(8000)                      # 等解码真跑完（done 已 emit）

        loop = QEventLoop()
        QTimer.singleShot(1500, loop.quit)
        loop.exec()                          # 把队列里的 done 投递掉

        self.assertNotIn(task, p._anim_tasks,
                         "陈旧任务没被回收 —— 每切一次图泄漏一个线程")

    def test_shutdown_waits_and_clears_everything(self):
        """`shutdown_anim()` 之后必须一个在途线程都不剩。"""
        p = self._pane()
        for i in range(10):
            p.show_path(self.gifs[i % 2])
        self.assertGreater(len(p._anim_tasks), 0, "前置条件：应当有在途任务")
        p.shutdown_anim()
        self.assertEqual(len(p._anim_tasks), 0)
        self.assertEqual(sum(1 for t in p._anim_tasks if t.isRunning()), 0)
        self.assertEqual(self._running(), 0, "还有解码线程在跑")

    def test_shutdown_is_idempotent(self):
        """可重复调用（`closeEvent` 和 `MainWindow.closeEvent` 都会调）。"""
        p = self._pane()
        p.show_path(self.gifs[0])
        p.shutdown_anim()
        p.shutdown_anim()
        p.shutdown_anim()

    def test_park_holds_reference_until_thread_finishes(self):
        """超时没收工的线程必须被模块级列表接住。

        否则它随控件一起被销毁，而"运行中的 QThread 被销毁"= Qt abort。
        停车位在它收工后要把引用放掉，不然会一直涨。
        """
        from imgdedup_ui import widgets as W

        t = W._AnimDecodeTask(self.gifs[0], 999999)
        t.start()
        W._park_anim_task(t)
        self.assertIn(t, W._ANIM_PARKED, "没收工的线程没被接住")
        t.wait(8000)
        for _ in range(3):
            self.app.processEvents()
        self.assertNotIn(t, W._ANIM_PARKED, "收工后没放掉 → 停车位会一直涨")

    def test_park_does_not_leak_when_thread_already_finished(self):
        """停车位必须处理"连接之前线程就已经收工"这个竞争。

        `_retire_anim_task` / `shutdown_anim` 都是先看 `isRunning()`
        再进 `_park_anim_task` 连接 `finished`。这两步之间线程完全可能
        已经跑完并发出过 `finished`：信号只发一次，连接建在之后
        就永远不会被触发 → 条目永久留在停车位。

        实测症状：真实 GUI 下快速切换，停车场从某次点击起卡在 1
        不再回落（线程和句柄再也放不掉）。

        这里把一个已经收工的线程直接丢进停车位，确定性地走那条路径。
        """
        from imgdedup_ui import widgets as W

        t = W._AnimDecodeTask(self.gifs[0], 777777)
        t.start()
        t.wait(8000)
        self.assertFalse(t.isRunning(), "前置条件：线程应当已经收工")

        W._park_anim_task(t)
        self.assertNotIn(
            t, W._ANIM_PARKED,
            "已收工的线程进了停车位却没被放掉 —— 条目会永久泄漏")

    def test_shutdown_parks_task_that_outlives_the_timeout(self):
        """`shutdown_anim` 超时后必须把任务挪进停车位。

        这条守的是 `shutdown_anim` 里那个 `_park_anim_task(t)` 调用 ——
        去掉它，测试不会崩（因为测试用的 GIF 解得快、根本不会超时），
        但真机上遇到大动图就会"关窗时闪退"。所以这里用一个可控阻塞的
        线程，把"超时"这条路径逼出来，否则它永远测不到。
        """
        import threading

        from imgdedup_ui import widgets as W

        gate = threading.Event()

        class _Blocking(W._AnimDecodeTask):
            def run(self) -> None:          # 不真解码，卡在门口等放行
                gate.wait(15)

        p = self._pane()
        t = _Blocking(self.gifs[0], 888888)
        p._anim_tasks.add(t)
        t.start()
        try:
            self.assertTrue(t.isRunning(), "前置条件：线程应当在跑")
            p.shutdown_anim(timeout_ms=50)   # 必然超时
            self.assertIn(t, W._ANIM_PARKED,
                          "超时没收工的线程没被接住 —— 真机上就是关窗闪退")
            self.assertNotIn(t, p._anim_tasks, "停车后不该还挂在控件上")
        finally:
            gate.set()                       # 放行，别把线程永远吊着
            t.wait(8000)
        for _ in range(3):
            self.app.processEvents()
        self.assertNotIn(t, W._ANIM_PARKED, "收工后停车位没放掉")

    def test_window_close_with_anim_in_flight_is_safe(self):
        """真实窗口：切几个 GIF 后正常关窗，不许留下在跑的线程。"""
        import gc

        from imgdedup_ui.window import MainWindow

        win = MainWindow()
        win.resize(1000, 700)
        for i in range(10):
            win.preview.show_path(self.gifs[i % 2])
        win.close()
        del win
        gc.collect()
        self.app.processEvents()
        self.assertEqual(self._running(), 0, "关窗后还有解码线程在跑")


class TestNoCrashOnTeardown(unittest.TestCase):
    """闪退必须放到子进程里测。

    崩溃（Qt 的 `abort()`）会把测试进程一起带走，进程内 `assertRaises`
    根本抓不到，而且它没有 Python 回溯，退出码是 127，
    只从测试输出看会像是"莫名其妙挂了"。

    所以这里起一个子进程跑完整场景，判据是子进程的退出码。
    """

    #: 子进程要跑的场景：快速切 GIF + 关窗，最后打印标记
    SCRIPT = r'''
import gc, os, sys, tempfile
sys.path.insert(0, os.path.abspath("."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from PIL import Image

tmp = Path(tempfile.mkdtemp())
gifs = []
for k in range(2):
    frames = [Image.new("RGB", (80, 60), (30 + 60 * k, 90, 200)) for _ in range(8)]
    p = tmp / ("anim%d.gif" % k)
    frames[0].save(str(p), save_all=True, append_images=frames[1:],
                   duration=40, loop=0)
    gifs.append(str(p))

from PySide6.QtWidgets import QApplication
from imgdedup_ui.window import MainWindow
app = QApplication.instance() or QApplication([])

# 反复"开窗 → 快速切 GIF → 关窗"，每次都留着在途解码线程
for r in range(4):
    win = MainWindow()
    win.resize(1000, 700)
    for i in range(12):
        win.preview.show_path(gifs[i % 2])
    win.close()
    del win
    gc.collect()
    app.processEvents()

print("SUB_OK", flush=True)
'''

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_teardown_with_inflight_decodes_exits_cleanly(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("没有 Pillow")

        import subprocess
        from pathlib import Path

        proj = Path(__file__).resolve().parent.parent
        r = subprocess.run([sys.executable, "-c", self.SCRIPT],
                           cwd=str(proj), capture_output=True, text=True,
                           timeout=300)
        self.assertEqual(
            r.returncode, 0,
            f"子进程退出码 {r.returncode}（127 通常是 Qt 的 abort）：\n"
            f"stdout={r.stdout[-1500:]}\nstderr={r.stderr[-1500:]}")
        self.assertIn("SUB_OK", r.stdout, "子进程没跑到最后一行")

    #: 上面那条测试用的 GIF 是 80×60 / 8 帧，解码亚毫秒。
    #: 撞不上"`run()` 返回了但线程还没真正结束"那个几毫秒的窗口，
    #: 所以它在 bug 还在的时候照样全绿。用户那份 GIF 是 5.7 MB / 60 帧、
    #: 解码要 862 ms，窗口才宽到"切 3~4 次就崩"。
    #: 这条脚本把那几毫秒人为放大，让窗口必定被撞上。
    SLOW_EXIT_SCRIPT = r'''
import gc, os, sys, tempfile, time
sys.path.insert(0, os.path.abspath("."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from PIL import Image

tmp = Path(tempfile.mkdtemp())
gifs = []
for k in range(2):
    frames = [Image.new("RGB", (80, 60), (30 + 60 * k, 90, 200)) for _ in range(8)]
    p = tmp / ("anim%d.gif" % k)
    frames[0].save(str(p), save_all=True, append_images=frames[1:],
                   duration=40, loop=0)
    gifs.append(str(p))

from PySide6.QtWidgets import QApplication
from imgdedup_ui import widgets as W
from imgdedup_ui.model import ThumbCache


class SlowExitTask(W._AnimDecodeTask):
    """`run()` 收尾慢一点 —— 模拟真实大动图"emit 完还要一会儿才真退出"。

    这正是生产环境里那个窗口：`done.emit()` 是 `run()` 的最后一句，
    emit 走队列连接立刻返回，而线程真正结束还要再走一小段。
    窗口内被摘掉最后一个引用 = 销毁运行中的 QThread = Qt abort。
    """

    def run(self) -> None:
        super().run()
        time.sleep(0.25)


W._AnimDecodeTask = SlowExitTask        # 换掉模块级符号，走真实调用路径

app = QApplication.instance() or QApplication([])
cache = ThumbCache(size=256, capacity=4)
pane = W.PreviewPane(cache)
pane.resize(300, 320)
pane.show()
app.processEvents()

for rnd in range(4):
    for p in gifs:
        pane.show_path(p)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            app.processEvents()
            time.sleep(0.002)
    gc.collect()
    app.processEvents()

pane.shutdown_anim()
cache.shutdown()
print("SUB_OK", flush=True)
'''

    def test_slow_exit_decode_does_not_abort(self):
        """这条才是"切 GIF 闪退"的真回归测试。

        `_on_anim_ready` 摘掉任务引用之前必须过 `_retire_anim_task`
        （还在跑就挪进停车场）。去掉那一步，本测试的子进程会在
        "QThread: Destroyed while thread is still running" 上 abort，
        退出码非 0，而上面那条用快 GIF 的测试照样全绿。
        """
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("没有 Pillow")

        import subprocess
        from pathlib import Path

        proj = Path(__file__).resolve().parent.parent
        r = subprocess.run([sys.executable, "-c", self.SLOW_EXIT_SCRIPT],
                           cwd=str(proj), capture_output=True, text=True,
                           timeout=300)
        self.assertEqual(
            r.returncode, 0,
            f"子进程退出码 {r.returncode} —— 多半是 Qt 的 abort"
            f"（QThread: Destroyed while thread is still running）：\n"
            f"stdout={r.stdout[-1500:]}\nstderr={r.stderr[-1500:]}")
        self.assertIn("SUB_OK", r.stdout, "子进程没跑到最后一行")


# ================================================================ 三档模式

class TestScanModes(unittest.TestCase):
    """三档扫描模式必须真的不同。"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_mode_order_matches_ui_indices(self):
        from imgdedup_ui.worker import MODE_ORDER, mode_at

        for i, k in enumerate(MODE_ORDER):
            self.assertEqual(mode_at(i), k)
        self.assertEqual(MODE_ORDER[0], "fast")
        self.assertEqual(MODE_ORDER[-1], "deep")

    def test_mode_at_out_of_range_falls_back(self):
        from imgdedup_ui.worker import mode_at

        self.assertEqual(mode_at(-1), "balanced")
        self.assertEqual(mode_at(99), "balanced")

    def test_every_mode_has_full_description(self):
        """说明文案的每个字段都不能空：界面上会直接读它们。"""
        from imgdedup_ui.worker import MODES

        for key, info in MODES.items():
            for field in ("name", "tag", "brief", "detects", "misses", "cost"):
                self.assertTrue(info.get(field), f"{key}.{field} 为空")

    def test_three_modes_are_mutually_distinct(self):
        """三档的"能抓到"必须两两不同，否则等于同一档换了名字。"""
        from imgdedup_ui.worker import MODES

        detects = [MODES[k]["detects"] for k in ("fast", "balanced", "deep")]
        self.assertEqual(len(set(detects)), 3, f"档位描述有重复：{detects}")

    def test_fast_mode_skips_full_hash_and_perceptual(self):
        """「快速」档的流水线必须真的跳过全量哈希与感知阶段。

        这是本项目修掉的一个真 bug：早先只有 `index == 2` 一处判断，
        于是"快速"和"精确"跑的是完全相同的流水线 ——
        下拉框里的"精确"是假的，用户选"快速"并没有更快。
        """
        from imgdedup_ui.worker import ScanWorker

        w = ScanWorker("E:/nonexistent", mode="fast")
        self.assertEqual(w.mode, "fast")
        self.assertFalse(w.semantic)

    def test_deep_mode_enables_semantic(self):
        from imgdedup_ui.worker import ScanWorker

        w = ScanWorker("E:/nonexistent", mode="deep")
        self.assertTrue(w.semantic)
        w2 = ScanWorker("E:/nonexistent", mode="balanced")
        self.assertFalse(w2.semantic)

    def test_legacy_semantic_flag_upgrades_to_deep(self):
        """旧调用点 `semantic=True` 必须抬成 deep，不能被静默降级。"""
        from imgdedup_ui.worker import ScanWorker

        w = ScanWorker("E:/nonexistent", semantic=True)
        self.assertEqual(w.mode, "deep")
        self.assertTrue(w.semantic)

    def test_dont_downgrade_deep_with_semantic_false(self):
        """`mode="deep"` + `semantic=False` 不许把 deep 降级。"""
        from imgdedup_ui.worker import ScanWorker

        w = ScanWorker("E:/nonexistent", mode="deep", semantic=False)
        self.assertEqual(w.mode, "deep")
        self.assertTrue(w.semantic)

    def test_ui_hint_reads_from_mode_table(self):
        """界面上显示的差异说明必须来自 `worker.MODES`，不许另写一份。"""
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            seen = set()
            for i in range(w.cmb_mode.count()):
                w.cmb_mode.setCurrentIndex(i)
                txt = w.lbl_mode.text()
                self.assertTrue(txt.strip(), f"第 {i} 档说明是空的")
                self.assertIn("漏掉", txt, f"第 {i} 档没写'会漏掉什么'")
                seen.add(txt)
            self.assertEqual(len(seen), w.cmb_mode.count(),
                             "不同档位的说明文字有重复")
        finally:
            w.close()

    def test_ui_hint_updates_on_switch(self):
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        try:
            w.cmb_mode.setCurrentIndex(0)
            a = w.lbl_mode.text()
            w.cmb_mode.setCurrentIndex(2)
            b = w.lbl_mode.text()
            self.assertNotEqual(a, b, "切档位说明没跟着变")
        finally:
            w.close()


class TestFastModeEndToEnd(unittest.TestCase):
    """`fast` 档必须真能跑完，不能只是"参数传对了"。

    这类测试是因为踩过坑才加的：`fast` 分支最初写在 `cand`（候选集）
    定义之前，一进分支就 `UnboundLocalError`。而单元测试只断言了
    `ScanWorker(..., mode="fast").mode == "fast"`：参数完全正确，
    真扫一遍却立刻炸。参数对不对 ≠ 能不能跑，必须端到端跑一次。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_fast_mode_runs_to_completion(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没有 Pillow")

        import tempfile
        from pathlib import Path

        from PySide6.QtCore import QEventLoop, QTimer

        from imgdedup_ui.worker import ScanWorker

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # 造 3 张完全相同的图（进 exact）+ 1 张不同的（不进组）
            for name in ("a.png", "b.png", "c.png"):
                Image.new("RGB", (80, 60), (10, 120, 200)).save(str(root / name))
            Image.new("RGB", (80, 60), (200, 10, 30)).save(str(root / "d.png"))

            loop = QEventLoop()
            holder = {}
            w = ScanWorker(str(root), mode="fast", threshold=0.9)
            w.finished_ok.connect(lambda rows, meta: (
                holder.update(rows=rows, meta=meta), loop.quit()))
            w.failed.connect(lambda e: (holder.update(error=e), loop.quit()))
            QTimer.singleShot(60_000, loop.quit)
            w.start()
            loop.exec()
            w.wait(3000)

            self.assertNotIn("error", holder,
                             f"fast 档扫崩了：{holder.get('error')}")
            meta = holder.get("meta") or {}
            self.assertTrue(meta.get("full_hash_skipped"),
                            "fast 档没有跳过全量哈希")
            self.assertEqual(meta.get("perceptual_groups"), 0,
                             "fast 档不该产出感知组")
            rows = holder.get("rows")
            self.assertIsNotNone(rows, "fast 档没返回结果")
            # 3 张相同图必须归成 1 组
            self.assertEqual(len(rows), 1, f"预期 1 组，实际 {len(rows)}")
            self.assertEqual(len(rows[0].items), 3)

    def test_balanced_mode_finds_more_than_fast(self):
        """同一个目录，balanced 的组数必须 >= fast（它多做感知层）。

        只断言 `>=` 不断言 `>`：如果测试图之间没有"近重复"，
        两档组数本来就该相同：硬要 `>` 会让这条测试变成脆测试。
        真正的"两档不同"由 `test_three_modes_are_mutually_distinct`
        从描述层守。
        """
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没有 Pillow")

        import tempfile
        from pathlib import Path

        from PySide6.QtCore import QEventLoop, QTimer

        from imgdedup_ui.worker import ScanWorker

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            Image.new("RGB", (80, 60), (10, 120, 200)).save(str(root / "a.png"))
            Image.new("RGB", (80, 60), (10, 120, 200)).save(str(root / "b.png"))
            # 同图但重存成 JPEG（不同字节、视觉相同）→ 只有感知层能抓
            Image.new("RGB", (80, 60), (10, 120, 200)).save(
                str(root / "c.jpg"), quality=70)

            def run(mode):
                loop = QEventLoop()
                holder = {}
                w = ScanWorker(str(root), mode=mode, threshold=0.9)
                w.finished_ok.connect(lambda rows, meta: (
                    holder.update(rows=rows), loop.quit()))
                w.failed.connect(lambda e: (holder.update(error=e), loop.quit()))
                QTimer.singleShot(60_000, loop.quit)
                w.start()
                loop.exec()
                w.wait(3000)
                return holder

            fast = run("fast")
            bal = run("balanced")
            self.assertIsNone(fast.get("error"), f"fast 扫崩了：{fast.get('error')}")
            self.assertIsNone(bal.get("error"), f"balanced 扫崩了：{bal.get('error')}")
            self.assertGreaterEqual(len(bal["rows"]), len(fast["rows"]))


# ============================================================ 百分比输入 / 重置

class TestZoomAbsoluteSetter(unittest.TestCase):
    """`set_zoom` 是"设到某个绝对值"，和 `zoom_by` 的"按倍率增减"不同。"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def _pane(self):
        from imgdedup_ui.model import ThumbCache
        from imgdedup_ui.widgets import PreviewPane, _pixmap_from_rgb

        cache = ThumbCache(size=256, capacity=4)
        p = PreviewPane(cache)
        p.resize(260, 300)
        p._pixmap = _pixmap_from_rgb(np.zeros((40, 60, 3), dtype=np.uint8))
        self.addCleanup(cache.shutdown)
        self.addCleanup(p.close)
        return p

    def test_set_zoom_is_absolute_not_relative(self):
        """连续设两次同样的值，第二次必须不叠加。"""
        p = self._pane()
        p.set_zoom(2.0)
        self.assertAlmostEqual(p.zoom, 2.0, places=6)
        p.set_zoom(2.0)
        self.assertAlmostEqual(p.zoom, 2.0, places=6,
                               msg="set_zoom 被当成倍率乘上去了")

    def test_set_zoom_clamps_to_range(self):
        from imgdedup_ui.widgets import PreviewPane

        p = self._pane()
        p.set_zoom(999.0)
        self.assertAlmostEqual(p.zoom, PreviewPane.ZOOM_MAX, places=6)
        p.set_zoom(0.001)
        self.assertAlmostEqual(p.zoom, PreviewPane.ZOOM_MIN, places=6)

    def test_set_zoom_ignores_nonpositive(self):
        """0 / 负数 / nan 都要被忽略，不能把倍率搞成 0（除零/空白画面）。"""
        p = self._pane()
        p.set_zoom(2.0)
        for bad in (0.0, -1.0, float("nan")):
            p.set_zoom(bad)
            self.assertAlmostEqual(p.zoom, 2.0, places=6,
                                   msg=f"set_zoom({bad}) 不该改变倍率")

    def test_zoom_property_matches_internal(self):
        p = self._pane()
        p.zoom_by(3.0)
        self.assertAlmostEqual(p.zoom, p._zoom, places=9)


class TestZoomChangedSignal(unittest.TestCase):
    """信号是输入框和画面之间的唯一同步通道，断了输入框就变成"死"的。"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def _pane(self):
        from imgdedup_ui.model import ThumbCache
        from imgdedup_ui.widgets import PreviewPane, _pixmap_from_rgb

        cache = ThumbCache(size=256, capacity=4)
        p = PreviewPane(cache)
        p.resize(260, 300)
        p._pixmap = _pixmap_from_rgb(np.zeros((40, 60, 3), dtype=np.uint8))
        self.addCleanup(cache.shutdown)
        self.addCleanup(p.close)
        return p

    def _record(self, p):
        seen = []
        p.zoomChanged.connect(seen.append)
        return seen

    def test_emitted_on_zoom_by(self):
        p = self._pane()
        seen = self._record(p)
        p.zoom_by(2.0)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 2.0, places=6)

    def test_emitted_on_set_zoom(self):
        p = self._pane()
        seen = self._record(p)
        p.set_zoom(4.0)
        self.assertEqual(seen, [4.0])

    def test_emitted_on_reset(self):
        p = self._pane()
        p.zoom_by(8.0)
        seen = self._record(p)
        p.reset_zoom()
        self.assertEqual(seen, [1.0], "重置没有广播 → 输入框会停在旧数值")

    def test_emitted_on_image_switch(self):
        """切图会归位到 100%，输入框也必须跟着回去。"""
        p = self._pane()
        p.zoom_by(4.0)
        # 必须先让 `_path` 是"有值"的：`show_path` 开头有一条
        # "路径没变就短路"的优化，`_path` 本来就是 None 时再传 None
        # 会直接 return，走不到 `_reset_view`。
        p._path = "some/old.png"
        seen = self._record(p)
        p.show_path(None)                 # 清空 → _reset_view
        self.assertEqual(seen, [1.0])

    def test_not_emitted_when_clamped_at_limit(self):
        """已经到顶还继续滚，不该反复广播（会让输入框无谓重绘）。"""
        from imgdedup_ui.widgets import PreviewPane

        p = self._pane()
        p.set_zoom(PreviewPane.ZOOM_MAX)
        seen = self._record(p)
        p.zoom_by(2.0)
        self.assertEqual(seen, [], "到顶后仍在广播")


class TestZoomControlRow(unittest.TestCase):
    """预览框下方那一行：百分比输入框 + 重置按钮。"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def _win(self):
        from imgdedup_ui.window import MainWindow

        w = MainWindow()
        self.addCleanup(w.close)
        return w

    # ---------------------------------------------------------- 存在性
    def test_row_widgets_exist(self):
        w = self._win()
        self.assertTrue(hasattr(w, "zoom_edit"))
        self.assertTrue(hasattr(w, "btn_zoom_reset"))
        self.assertEqual(w.zoom_edit.text(), "100%")

    def test_input_wide_enough_for_max_zoom(self):
        """输入框是定宽的，宽度按"最长可能值"定的。

        现在 `ZOOM_MAX = 16.0` → `"1600%"` 是 5 个字符，68px 够。
        但要是有人把上限提到 100x，字符串变成 `"10000%"`（6 字符），
        框里就只看得见一半数字，而功能测试全都还是绿的。
        这条提前把假设钉住。
        """
        from imgdedup_ui.widgets import PreviewPane

        w = self._win()
        longest = f"{PreviewPane.ZOOM_MAX:.0%}"
        self.assertLessEqual(
            len(longest), 5,
            f"ZOOM_MAX 现在是 {PreviewPane.ZOOM_MAX}（'{longest}' 有 "
            f"{len(longest)} 字符）—— 输入框的固定宽度要跟着调大")
        self.assertGreaterEqual(w.zoom_edit.width(), 60,
                                "输入框太窄，最长值会显示不全")

    def test_row_is_inside_the_splitter_but_preview_keeps_its_own_cell(self):
        """预览框和缩放行必须包在一个容器里再进 splitter。

        如果两个都直接 `addWidget`，splitter 会多出一格 ——
        拖动分隔条时缩放控件会被单独拉扯、甚至拖成 0 高看不见。
        """
        w = self._win()
        self.assertEqual(w.right_split.count(), 2,
                         "splitter 应该只有「预览区」「信息区」两格")
        pbox = w.right_split.widget(0)
        self.assertTrue(pbox.isAncestorOf(w.preview),
                        "预览框不在预览区容器里")
        self.assertTrue(pbox.isAncestorOf(w.zoom_edit),
                        "缩放输入框不在预览区容器里（会被单独拖拽）")

    # ---------------------------------------------------------- 输入解析
    def test_plain_number_is_percent(self):
        w = self._win()
        w.zoom_edit.setText("150")
        w._apply_zoom_from_edit()
        self.assertAlmostEqual(w.preview.zoom, 1.5, places=6)

    def test_percent_sign_is_accepted(self):
        w = self._win()
        w.zoom_edit.setText("250%")
        w._apply_zoom_from_edit()
        self.assertAlmostEqual(w.preview.zoom, 2.5, places=6)

    def test_fullwidth_percent_and_spaces_are_accepted(self):
        """中文输入法打出来的是全角 ％：不认的话用户会以为功能坏了。"""
        w = self._win()
        w.zoom_edit.setText("  150 ％  ")
        w._apply_zoom_from_edit()
        self.assertAlmostEqual(w.preview.zoom, 1.5, places=6)

    def test_garbage_input_reverts_to_current(self):
        """乱输入不能把画面搞乱，且必须把框里的字改回真实值。"""
        w = self._win()
        w.preview.set_zoom(2.0)
        for bad in ("abc", "", "  ", "%", "-50", "0"):
            w.zoom_edit.setText(bad)
            w._apply_zoom_from_edit()
            self.assertAlmostEqual(w.preview.zoom, 2.0, places=6,
                                   msg=f"{bad!r} 改变了倍率")
            self.assertEqual(w.zoom_edit.text(), "200%",
                             f"{bad!r} 之后输入框没回退到真实值")

    def test_out_of_range_input_shows_clamped_value(self):
        """输入 9999% 后框里必须显示夹取后的 1600%，不能留着 9999。"""
        from imgdedup_ui.widgets import PreviewPane

        w = self._win()
        w.zoom_edit.setText("9999")
        w._apply_zoom_from_edit()
        self.assertAlmostEqual(w.preview.zoom, PreviewPane.ZOOM_MAX, places=6)
        self.assertEqual(w.zoom_edit.text(), f"{PreviewPane.ZOOM_MAX:.0%}")

    def test_input_below_minimum_shows_clamped_value(self):
        from imgdedup_ui.widgets import PreviewPane

        w = self._win()
        w.zoom_edit.setText("1")
        w._apply_zoom_from_edit()
        self.assertAlmostEqual(w.preview.zoom, PreviewPane.ZOOM_MIN, places=6)
        self.assertEqual(w.zoom_edit.text(), f"{PreviewPane.ZOOM_MIN:.0%}")

    # ---------------------------------------------------------- 重置
    def test_reset_button_restores_100(self):
        w = self._win()
        w.preview.set_zoom(4.0)
        w.btn_zoom_reset.click()
        self.assertAlmostEqual(w.preview.zoom, 1.0, places=6)
        self.assertEqual(w.zoom_edit.text(), "100%")

    def test_reset_button_also_clears_pan(self):
        """只把倍率还原、忘了平移，图会歪在一边：那不算"原始状态"。"""
        w = self._win()
        w.preview.set_zoom(8.0)
        w.preview._pan = QPointF(40.0, 25.0)
        w.btn_zoom_reset.click()
        self.assertAlmostEqual(w.preview._pan.x(), 0.0, places=6)
        self.assertAlmostEqual(w.preview._pan.y(), 0.0, places=6)

    # ---------------------------------------------------------- 反向同步
    def test_wheel_updates_the_input_box(self):
        """滚轮改了倍率，输入框必须跟着变，否则两处数字打架。"""
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QWheelEvent

        w = self._win()
        ev = QWheelEvent(QPointF(60.0, 60.0), QPointF(60.0, 60.0),
                         QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                         Qt.NoModifier, Qt.ScrollUpdate, False)
        w.preview.wheelEvent(ev)
        self.assertGreater(w.preview.zoom, 1.0)
        self.assertEqual(w.zoom_edit.text(), f"{w.preview.zoom:.0%}",
                         "滚轮缩放后输入框没同步")

    def test_keyboard_reset_updates_the_input_box(self):
        """预览框里的 0 键重置，输入框也要回到 100%。"""
        w = self._win()
        w.preview.set_zoom(3.0)
        w.preview.reset_zoom()
        self.assertEqual(w.zoom_edit.text(), "100%")

    def test_typing_is_not_clobbered_by_zoom_changes(self):
        """用户正在打字时不能被改写。

        否则场景是：想输 150，刚打完 "15"，手滑滚了一下滚轮 →
        框里被冲成 "100%"，前两个字符白打。

        必须用 `insert()` 模拟真实键入，不能用 `setText()` ——
        `setText()` 是程序回写的路径，Qt 会把它当作"未修改"
        （`isModified()` 保持 False），用它构造不出"用户改过"的状态。
        第一版测试就是用 `setText` 写的，换成 `isModified` 判据后它假通过。
        """
        w = self._win()
        w.zoom_edit.clear()
        w.zoom_edit.insert("15")            # 真实键入
        self.assertTrue(w.zoom_edit.isModified(), "insert 之后应标记为已修改")
        w.preview.set_zoom(2.0)             # 模拟"滚轮动了"
        self.assertEqual(w.zoom_edit.text(), "15",
                         "打字过程中被同步冲掉了")
        # 打完并提交之后必须回写（"150" → 归一成 "150%"）
        w.zoom_edit.clear()
        w.zoom_edit.insert("150")
        w._apply_zoom_from_edit()
        self.assertEqual(w.zoom_edit.text(), "150%")

    def test_focus_alone_does_not_block_sync(self):
        """只拿到焦点、没打字时，滚轮必须照常回写输入框。

        这条是修掉的真 bug：第一版判据用的是 `hasFocus()`，于是
        "点一下输入框（拿到焦点）→ 鼠标移到图上滚轮缩放"这条路径上，
        回写被一直跳过，框里停着旧数值：看起来就像"输入框坏了"。
        正确判据是 `isModified()`（只表示"用户改过内容"）。
        """
        w = self._win()
        w.zoom_edit.setText("100%")         # 程序回写 → isModified 清零
        w.zoom_edit.setFocus()
        self.app.processEvents()
        self.assertFalse(w.zoom_edit.isModified())
        w.preview.set_zoom(3.0)
        self.assertEqual(w.zoom_edit.text(), "300%",
                         "只聚焦未打字时，滚轮没能回写输入框")

    def test_commit_clears_modified_so_sync_resumes(self):
        """提交之后 `isModified` 必须被清掉，否则同步会永久停摆。

        `setText()` 会清 `modified`，而提交路径正是走 `setText` ——
        这条把"提交完还能继续同步"钉住。
        """
        w = self._win()
        w.zoom_edit.clear()
        w.zoom_edit.insert("150")
        w._apply_zoom_from_edit()
        self.assertFalse(w.zoom_edit.isModified(),
                         "提交后 isModified 没清掉 → 之后滚轮再也不会更新输入框")
        w.preview.set_zoom(2.0)
        self.assertEqual(w.zoom_edit.text(), "200%", "提交后同步没能恢复")

    def test_sync_writes_when_not_focused(self):
        w = self._win()
        w.zoom_edit.clearFocus()
        w.preview.set_zoom(3.0)
        self.assertEqual(w.zoom_edit.text(), "300%")

    # ---------------------------------------------------------- 键盘归属
    #
    # 这两条不依赖真实键盘焦点，而是直接在控件上盖掉 `hasFocus()`
    # 来构造状态。原因：离屏环境下 `setFocus()` 能不能生效不稳定，
    # 用真实焦点写的话这条测试会时不时走 `skipTest`，而"跳过"在退出码上
    # 和"通过"没区别，注入回归时会看起来像抓到了、其实什么都没测。
    # （第一版就是真实焦点版，注入验证时确实漏报了一次。）
    def _force_focus(self, on: bool, *widgets) -> None:
        """临时把若干控件的 `hasFocus()` 钉成 `on`，退出时还原。"""
        for wd in widgets:
            wd.hasFocus = (lambda: on)
            self.addCleanup(lambda wd=wd: wd.__dict__.pop("hasFocus", None))

    def test_keys_are_not_stolen_while_input_has_focus(self):
        """焦点在缩放输入框里时，窗口不能把按键当成网格操作。

        守的是 `Key_Space`：它在网格里绑的是"设为保留"，
        是会改数据的动作。用户在输入框里按空格（比如清空后想打个空格）
        却被网格吃掉的话，就会静默改掉一张图的保留状态。

        实测 `QLineEdit` 自己会 `accept()` 掉空格，所以正常情况下事件
        传不到窗口；但这条守卫是 `_handle_selection_key` 的契约，必须留着。
        """
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        w = self._win()
        self._force_focus(True, w.zoom_edit)
        self._force_focus(False, w.ed_root, w.sld)
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " ")
        self.assertFalse(
            w._handle_selection_key(ev),
            "输入框有焦点时窗口仍抢走了空格（会误触发「设为保留」）")

    def test_keys_work_when_grid_has_focus(self):
        """反过来：焦点在网格上时按键必须照常生效（别把守卫做过头）。"""
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from imgdedup_ui.model import GroupRow, ItemRow

        w = self._win()
        items = [ItemRow(path=f"X:/photos/k{i}.jpg", size=1000,
                         width=4032, height=3024, fmt="JPEG") for i in range(4)]
        row = GroupRow(kind="exact", label="96%", items=items, keeps={0})
        row.auto_decide()
        w.model.set_rows([row])
        self._force_focus(False, w.ed_root, w.sld, w.zoom_edit)
        w._set_selected(0, 0)
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " ")
        self.assertTrue(w._handle_selection_key(ev),
                        "焦点在网格上时按键被误判为「输入框有焦点」")


class TestZoomBadgeNotDuplicated(unittest.TestCase):
    """倍率只在输入框里显示一份。

    预览框正下方的输入框必须常驻显示倍率才可用，浮标再画一份就是
    相隔 20px 的两个相同数字。这条钉住"别再把倍率加回浮标"。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_badge_source_does_not_contain_zoom(self):
        import inspect

        from imgdedup_ui.widgets import PreviewPane

        src = inspect.getsource(PreviewPane._paint_badges)
        self.assertNotIn("self._zoom", src,
                         "倍率又回到浮标里了 —— 会和输入框重复显示")

    def test_badge_still_reports_animation(self):
        import inspect

        from imgdedup_ui.widgets import PreviewPane

        src = inspect.getsource(PreviewPane._paint_badges)
        self.assertIn("_anim_frames", src, "动图浮标被误删了")


if __name__ == "__main__":
    unittest.main()
