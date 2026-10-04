"""`imgdedup_ui/widgets.py` 的 `PreviewPane`：缩放数学与平移夹取。

不给它喂真实图片 —— 直接塞一个 `QPixmap`，只验数学。真实解码与动图
由 `--interact-test` 和 `tests/test_decode.py` 各自覆盖。
"""

import unittest

from tests import _qt

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPixmap

from imgdedup_ui import model as M
from imgdedup_ui import widgets as W


class PaneCase(unittest.TestCase):

    def setUp(self):
        self.app = _qt.app()
        self.cache = M.ThumbCache()
        self.pane = W.PreviewPane(self.cache)
        self.pane.resize(400, 400)

    def tearDown(self):
        self.cache.shutdown()

    def _with_pixmap(self, w=400, h=300):
        self.pane._pixmap = QPixmap(w, h)
        return self.pane


class TestConstants(PaneCase):

    def test_zoom_bounds(self):
        self.assertEqual(W.PreviewPane.ZOOM_MIN, 0.2)
        self.assertEqual(W.PreviewPane.ZOOM_MAX, 16.0)
        self.assertGreater(W.PreviewPane.ZOOM_STEP, 1.0)
        self.assertGreater(W.PreviewPane.STILL_SIDE, 0)


class TestInitialState(PaneCase):

    def test_starts_at_fit(self):
        self.assertEqual(self.pane.zoom, 1.0)
        self.assertEqual(self.pane._pan, QPointF(0.0, 0.0))

    def test_content_size_is_empty_without_a_pixmap(self):
        self.assertEqual(self.pane._content_size().width(), 0)
        self.assertEqual(self.pane._content_size().height(), 0)


class TestSetZoom(PaneCase):

    def test_absolute_set(self):
        self._with_pixmap()
        self.pane.set_zoom(2.0)
        self.assertAlmostEqual(self.pane.zoom, 2.0)

    def test_clamped_at_the_upper_bound(self):
        self._with_pixmap()
        self.pane.set_zoom(10_000.0)
        self.assertEqual(self.pane.zoom, W.PreviewPane.ZOOM_MAX)

    def test_clamped_at_the_lower_bound(self):
        self._with_pixmap()
        self.pane.set_zoom(0.0001)
        self.assertEqual(self.pane.zoom, W.PreviewPane.ZOOM_MIN)

    def test_non_positive_is_ignored(self):
        self._with_pixmap()
        self.pane.set_zoom(3.0)
        self.pane.set_zoom(0.0)
        self.pane.set_zoom(-5.0)
        self.assertAlmostEqual(self.pane.zoom, 3.0)

    def test_signal_carries_the_new_value(self):
        self._with_pixmap()
        seen = []
        self.pane.zoomChanged.connect(seen.append)
        self.pane.set_zoom(2.5)
        self.assertEqual(seen[-1], self.pane.zoom)


class TestZoomBy(PaneCase):

    def test_multiplies(self):
        self._with_pixmap()
        self.pane.zoom_by(2.0)
        self.assertAlmostEqual(self.pane.zoom, 2.0)
        self.pane.zoom_by(0.5)
        self.assertAlmostEqual(self.pane.zoom, 1.0)

    def test_no_op_does_not_emit(self):
        self._with_pixmap()
        seen = []
        self.pane.zoomChanged.connect(seen.append)
        self.pane.zoom_by(1.0)
        self.assertEqual(seen, [])

    def test_saturates_without_going_past_the_bounds(self):
        self._with_pixmap()
        for _ in range(200):
            self.pane.zoom_by(W.PreviewPane.ZOOM_STEP)
        self.assertEqual(self.pane.zoom, W.PreviewPane.ZOOM_MAX)
        for _ in range(400):
            self.pane.zoom_by(1.0 / W.PreviewPane.ZOOM_STEP)
        self.assertEqual(self.pane.zoom, W.PreviewPane.ZOOM_MIN)


class TestReset(PaneCase):

    def test_reset_clears_zoom_and_pan(self):
        self._with_pixmap()
        self.pane.set_zoom(4.0)
        self.pane._pan = QPointF(30.0, -20.0)
        self.pane.reset_zoom()
        self.assertEqual(self.pane.zoom, 1.0)
        self.assertEqual(self.pane._pan, QPointF(0.0, 0.0))


class TestClampPan(PaneCase):

    def _bounds(self):
        c = self.pane._content_size()
        avail = self.pane.rect().adjusted(11, 11, -11, -11)
        return (max(0.0, (c.width() - avail.width()) / 2.0),
                max(0.0, (c.height() - avail.height()) / 2.0))

    def test_small_image_cannot_be_panned(self):
        self._with_pixmap(100, 100)
        self.pane.set_zoom(1.0)
        self.pane._pan = QPointF(500.0, 500.0)
        self.pane._clamp_pan()
        self.assertEqual(self.pane._pan, QPointF(0.0, 0.0))

    def test_pan_is_clamped_to_the_content_edges(self):
        self._with_pixmap()
        self.pane.set_zoom(4.0)
        mx, my = self._bounds()
        self.assertGreater(mx, 0.0)
        self.assertGreater(my, 0.0)
        self.pane._pan = QPointF(10_000.0, -10_000.0)
        self.pane._clamp_pan()
        self.assertAlmostEqual(self.pane._pan.x(), mx, delta=1.0)
        self.assertAlmostEqual(self.pane._pan.y(), -my, delta=1.0)

    def test_pan_inside_the_bounds_is_left_alone(self):
        self._with_pixmap()
        self.pane.set_zoom(4.0)
        self.pane._pan = QPointF(5.0, -5.0)
        self.pane._clamp_pan()
        self.assertEqual(self.pane._pan, QPointF(5.0, -5.0))

    def test_zooming_out_pulls_the_pan_back(self):
        self._with_pixmap()
        self.pane.set_zoom(8.0)
        self.pane._pan = QPointF(10_000.0, 10_000.0)
        self.pane._clamp_pan()
        self.assertNotEqual(self.pane._pan, QPointF(0.0, 0.0))
        self.pane.set_zoom(1.0)
        self.assertEqual(self.pane._pan, QPointF(0.0, 0.0))


class TestShowPath(PaneCase):

    def test_none_clears_everything(self):
        self.pane.show_path("C:/imgs/a.png")
        self.assertEqual(self.pane._path, "C:/imgs/a.png")
        self.pane.show_path(None)
        self.assertIsNone(self.pane._path)
        self.assertIsNone(self.pane._pixmap)

    def test_switching_path_resets_the_view(self):
        self._with_pixmap()
        self.pane.set_zoom(4.0)
        self.pane.show_path("C:/imgs/b.png")
        self.assertEqual(self.pane.zoom, 1.0)
        self.assertEqual(self.pane._pan, QPointF(0.0, 0.0))

    def test_repeated_same_path_does_not_reset_the_view(self):
        self._with_pixmap()
        self.pane.show_path("C:/imgs/a.png")
        self.pane._pixmap = QPixmap(400, 300)
        self.pane.set_zoom(3.0)
        self.pane.show_path("C:/imgs/a.png")
        self.assertAlmostEqual(self.pane.zoom, 3.0)


if __name__ == "__main__":
    unittest.main()
