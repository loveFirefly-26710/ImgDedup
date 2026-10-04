"""滚轮手感：一格 = 一个 `WHEEL_STEP_PX`，触控板走 `pixelDelta`。

Qt 默认一格滚 266px（整屏），卡片高不到 200px —— 用户会觉得「一滚就跳过头」。
这里的换算把三件事钉住：鼠标一格固定步长、触控板按像素且限幅、横向滚轮当垂直用。
"""

import unittest

from tests import _qt

from PySide6.QtCore import QPoint

from imgdedup_ui import window as W


class _FakeWheel:
    """只实现 `_on_wheel` 真正用到的那几个方法。"""

    def __init__(self, *, angle=(0, 0), pixel=(0, 0)):
        self._angle = QPoint(*angle)
        self._pixel = QPoint(*pixel)
        self.accepted = False

    def angleDelta(self) -> QPoint:
        return self._angle

    def pixelDelta(self) -> QPoint:
        return self._pixel

    def accept(self) -> None:
        self.accepted = True


class WheelCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _qt.app()

    def setUp(self):
        self.win = W.MainWindow()
        self.sb = self.win.view.verticalScrollBar()
        self.sb.setRange(0, 10_000)
        self.base = 5_000
        self.sb.setValue(self.base)

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        _qt.flush()

    def scroll(self, **kw) -> int:
        """滚一格，返回滚动条相对**滚动前**位置的变化量。"""
        before = self.sb.value()
        event = _FakeWheel(**kw)
        self.assertTrue(self.win._on_wheel(event))
        self.assertTrue(event.accepted)
        return self.sb.value() - before


class TestConstants(unittest.TestCase):

    def test_step_is_one_card_not_one_screen(self):
        # Qt 默认是 266px，这里刻意压到一个卡片的高度量级
        self.assertGreater(W.WHEEL_STEP_PX, 0)
        self.assertLess(W.WHEEL_STEP_PX, 266)

    def test_notch_is_the_standard_wheel_delta(self):
        self.assertEqual(W.WHEEL_NOTCH, 120)


class TestMouseWheel(WheelCase):

    def test_one_notch_down(self):
        self.assertEqual(self.scroll(angle=(0, -W.WHEEL_NOTCH)), W.WHEEL_STEP_PX)

    def test_one_notch_up(self):
        self.assertEqual(self.scroll(angle=(0, W.WHEEL_NOTCH)), -W.WHEEL_STEP_PX)

    def test_small_delta_is_not_truncated_to_zero(self):
        """有些鼠标一格只发 ±30，`int(delta/120)` 会是 0。

        那时必须兜底成「一格」，否则滚轮完全没反应。
        """
        self.assertEqual(self.scroll(angle=(0, -30)), W.WHEEL_STEP_PX)
        self.assertEqual(self.scroll(angle=(0, 30)), -W.WHEEL_STEP_PX)

    def test_multiple_notches_scale(self):
        self.assertEqual(self.scroll(angle=(0, -W.WHEEL_NOTCH * 3)),
                         W.WHEEL_STEP_PX * 3)

    def test_horizontal_wheel_acts_like_vertical(self):
        self.assertEqual(self.scroll(angle=(-W.WHEEL_NOTCH, 0)), W.WHEEL_STEP_PX)

    def test_does_not_overshoot_the_end(self):
        self.sb.setValue(0)
        event = _FakeWheel(angle=(0, W.WHEEL_NOTCH * 10))
        self.win._on_wheel(event)
        self.assertEqual(self.sb.value(), 0)


class TestTrackpad(WheelCase):

    def test_pixel_delta_is_used_directly(self):
        self.assertEqual(self.scroll(pixel=(0, -9)), 9)

    def test_pixel_delta_is_clamped_to_one_step(self):
        """触控板一甩能给出几百像素，不夹的话会直接飞过去。"""
        self.assertEqual(self.scroll(pixel=(0, -300)), W.WHEEL_STEP_PX)
        self.assertEqual(self.scroll(pixel=(0, 300)), -W.WHEEL_STEP_PX)

    def test_pixel_delta_wins_over_angle_delta(self):
        self.assertEqual(self.scroll(angle=(0, -W.WHEEL_NOTCH), pixel=(0, -3)), 3)


class TestNoScrollableContent(WheelCase):

    def test_event_is_swallowed_when_there_is_nothing_to_scroll(self):
        self.sb.setRange(0, 0)
        event = _FakeWheel(angle=(0, -W.WHEEL_NOTCH))
        self.assertTrue(self.win._on_wheel(event))
        self.assertEqual(self.sb.value(), 0)

    def test_empty_event_is_ignored(self):
        event = _FakeWheel()
        self.assertTrue(self.win._on_wheel(event))
        self.assertEqual(self.sb.value(), self.base)


if __name__ == "__main__":
    unittest.main()
