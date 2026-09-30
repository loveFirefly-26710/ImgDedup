"""滚轮滚动节奏。

这是用户直接反馈的体验问题，必须有测试钉住：靠肉眼验证不了
"一格滚轮到底走了多少像素"。

背景：`QListView` 在 `ScrollPerPixel` 模式下，Qt 会把滚动条的 `singleStep`
设成"一个 item 的高度"。而这里的 item 是一整行卡片（缩略图 112px 时
一行约 1018px），滚轮再乘系统 `wheelScrollLines`（默认 3）
→ 一格滚轮位移 3000px 以上，而一屏通常只有几百像素高
→ 一次滚动直接见底，中间的分组全被跳过。

修法是接管滚轮事件，把每格固定成 `WHEEL_STEP_PX`。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from imgdedup_ui.model import GroupRow  # noqa: E402
from imgdedup_ui.window import WHEEL_NOTCH, WHEEL_STEP_PX, MainWindow  # noqa: E402


def _many_groups(n: int = 12, per: int = 6) -> list[GroupRow]:
    """造 n 个分组，每组 per 张：保证有足够长的滚动范围可测。"""
    from imgdedup_core.scan import FileEntry
    from imgdedup_ui.model import build_rows

    entries = []
    groups = []
    for g in range(n):
        base = len(entries)
        for i in range(per):
            entries.append(FileEntry(path=f"E:/fake/g{g}/p{i}.png",
                                     size=1000 + i, mtime_ns=0, ext=".png"))
        groups.append(list(range(base, base + per)))
    return build_rows(entries, groups, kind="exact")


class TestWheelScroll(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = MainWindow()
        self.w.resize(1360, 880)
        self.w.model.set_rows(_many_groups())
        self.w.show()
        self.app.processEvents()
        self.w._sync_view_width()
        self.sb = self.w.view.verticalScrollBar()

    def tearDown(self):
        # 必须收线程池，否则后台缩略图任务回来时信号已销毁 →
        # 进程退出码 139（段错误），而测试看上去是全绿的。
        self.w.thumbs.shutdown()
        self.w.preview_cache.shutdown()
        self.w.close()

    def _wheel(self, delta: int = 0, pix: int = 0, dx: int = 0) -> int:
        """投递真实 QWheelEvent，返回滚动条位移。"""
        before = self.sb.value()
        pt = QPoint(100, 100)
        ev = QWheelEvent(pt, self.w.view.viewport().mapToGlobal(pt),
                         QPoint(0, pix), QPoint(dx, delta),
                         Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        self.app.sendEvent(self.w.view.viewport(), ev)
        self.app.processEvents()
        return self.sb.value() - before

    # ------------------------------------------------------------ 场景
    def test_has_something_to_scroll(self):
        """前置条件：测试数据必须真的能滚，否则后面全在测空气。"""
        self.assertGreater(self.sb.maximum(), 0,
                           "测试数据没造出可滚动的高度，断言无意义")

    def test_one_notch_moves_exactly_step(self):
        """一格标准滚轮 = 固定 52px，与卡片多高无关。"""
        self.assertEqual(self._wheel(delta=-WHEEL_NOTCH), WHEEL_STEP_PX)
        self.assertEqual(self._wheel(delta=WHEEL_NOTCH), -WHEEL_STEP_PX)

    def test_wheel_far_slower_than_qt_default(self):
        """钉住这次修复：一格位移必须远小于 Qt 默认的单步（一行卡片高）。

        这是用户报的"一次滚动跳过多个页面"的量化判据。
        """
        step_hint = self.w.delegate.sizeHint(
            None, self.w.view.model().index(0, 0)).height()
        moved = abs(self._wheel(delta=-WHEEL_NOTCH))
        self.assertLess(moved, step_hint / 3,
                        f"一格滚轮走了 {moved}px，而一行卡片才 {step_hint}px —— "
                        f"又会跳过整行")

    def test_many_notches_needed_to_reach_bottom(self):
        """滚到底需要多格：一格就到底说明还是太快。"""
        total = self.sb.maximum()
        notches = 0
        while self.sb.value() < total and notches < 500:
            self._wheel(delta=-WHEEL_NOTCH)
            notches += 1
        self.assertGreater(notches, 2,
                           "两三格就滚到底了，用户依然会漏看中间的分组")
        # 每格位移稳定，不会忽快忽慢
        self.assertAlmostEqual(total / notches, WHEEL_STEP_PX, delta=WHEEL_STEP_PX)

    def test_direction_is_natural(self):
        """向下滚（delta<0）→ 内容上移（value 增大）。方向反了用起来极别扭。"""
        self.assertGreater(self._wheel(delta=-WHEEL_NOTCH), 0)
        self.assertLess(self._wheel(delta=WHEEL_NOTCH), 0)

    def test_trackpad_pixel_delta_is_preserved(self):
        """触控板：小像素增量原样走，保证丝滑而不是一格一格顿。"""
        self.assertEqual(self._wheel(pix=-9), 9)

    def test_trackpad_huge_delta_is_clamped(self):
        """触控板猛滑一下给几百像素：必须限幅，否则照样飞过去。"""
        self.assertEqual(self._wheel(pix=-300), WHEEL_STEP_PX)

    def test_tiny_angle_delta_still_moves(self):
        """慢速微拨 delta 不到一格：不能截断成 0 导致"滚不动"。"""
        self.assertEqual(self._wheel(delta=-30), WHEEL_STEP_PX)

    def test_trackpad_without_angle_delta_is_not_swallowed(self):
        """触控板事件常常 `angleDelta` 为 0、只有 `pixelDelta`。

        早期实现里先判断 `angleDelta == 0` 就 return True，
        把触控板事件整个吃掉了（表现为"触控板滚不动"）。
        """
        self.assertNotEqual(self._wheel(delta=0, pix=-20), 0,
                            "angleDelta 为 0 时把触控板事件吃掉了")

    def test_horizontal_wheel_used_as_vertical(self):
        """横向滚轮在网格里没有横向滚动条，当纵滚用比什么都不做好。"""
        self.assertEqual(self._wheel(dx=-WHEEL_NOTCH), WHEEL_STEP_PX)

    def test_no_scroll_when_nothing_to_scroll(self):
        """列表装得下时滚轮应被无害吃掉，而不是报错或乱动。"""
        self.w.model.set_rows([])
        self.app.processEvents()
        self.assertEqual(self._wheel(delta=-WHEEL_NOTCH), 0)


if __name__ == "__main__":
    unittest.main()
