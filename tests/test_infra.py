"""`imgdedup_core/infra.py`：格式化、内存闸门、预算、计时与进度。

这一层不碰 Qt、不碰 cv2，所以这些用例在任何环境下都能跑。
"""

import io
import os
import unittest
from contextlib import redirect_stdout

from imgdedup_core import infra as I


class TestHumanFormat(unittest.TestCase):

    def test_bytes(self):
        cases = [
            (0, "0 B"),
            (1, "1 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024 ** 2, "1.0 MB"),
            (1024 ** 3, "1.0 GB"),
            (1024 ** 4, "1.0 TB"),
            (1024 ** 5, "1.0 PB"),
        ]
        for n, want in cases:
            with self.subTest(n=n):
                self.assertEqual(I.human_bytes(n), want)

    def test_bytes_accepts_float(self):
        self.assertEqual(I.human_bytes(1536.0), "1.5 KB")

    def test_secs(self):
        self.assertEqual(I.human_secs(0.5), "500 ms")
        self.assertEqual(I.human_secs(2.5), "2.50 s")
        self.assertEqual(I.human_secs(60.0), "1m 0s")
        self.assertEqual(I.human_secs(90.0), "1m 30s")

    def test_rate_uses_thousands_separator(self):
        self.assertEqual(I.human_rate(1234.5), "1,234/s")
        self.assertEqual(I.human_rate(0), "0/s")


class TestMemGate(unittest.TestCase):

    def test_peak_tracks_high_water_mark(self):
        gate = I.MemGate(1000)
        self.assertEqual(gate.peak, 0)
        with gate.reserve(300):
            self.assertEqual(gate.peak, 300)
            with gate.reserve(400):
                self.assertEqual(gate.peak, 700)
        # 释放后峰值不回落 —— 它是历史最高水位，不是当前占用
        self.assertEqual(gate.peak, 700)

    def test_need_is_clamped_to_budget(self):
        gate = I.MemGate(100)
        with gate.reserve(10 ** 9):
            self.assertEqual(gate.peak, 100)

    def test_budget_is_at_least_one(self):
        gate = I.MemGate(0)
        with gate.reserve(5):
            self.assertEqual(gate.peak, 1)

    def test_negative_need_becomes_one(self):
        gate = I.MemGate(10)
        with gate.reserve(-5):
            self.assertEqual(gate.peak, 1)


class TestBudgets(unittest.TestCase):

    def test_default_memory_budget_is_capped(self):
        cap = 2 * 1024 ** 3
        b = I.default_memory_budget()
        self.assertGreater(b, 0)
        self.assertLessEqual(b, cap)

    def test_default_memory_budget_honours_explicit_cap(self):
        self.assertLessEqual(I.default_memory_budget(cap_bytes=12345), 12345)

    def test_cpu_budget_within_cpu_count(self):
        n = os.cpu_count() or 4
        self.assertGreaterEqual(I.cpu_budget(0.5), 1)
        self.assertLessEqual(I.cpu_budget(1.0), n)
        self.assertGreaterEqual(I.cpu_budget(0.0), 1)


class TestTimings(unittest.TestCase):

    def test_add_accumulates_and_items_sorts_desc(self):
        t = I.Timings()
        t.add("a", 1.5)
        t.add("a", 0.5)
        t.add("b", 0.2)
        self.assertEqual(t.items(), [("a", 2.0), ("b", 0.2)])
        self.assertAlmostEqual(t.total(), 2.2)

    def test_empty(self):
        t = I.Timings()
        self.assertEqual(t.items(), [])
        self.assertEqual(t.total(), 0.0)


class TestProgress(unittest.TestCase):

    def test_quiet_prints_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            p = I.Progress("x", 10, every=5, quiet=True)
            for _ in range(10):
                p.tick()
        self.assertEqual(buf.getvalue(), "")

    def test_prints_on_every_boundary_and_at_total(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            p = I.Progress("x", 10, every=5)
            for _ in range(10):
                p.tick()
        out = buf.getvalue()
        self.assertEqual(out.count("[x]"), 2)
        self.assertIn("10/10", out)
        self.assertIn("100.0%", out)

    def test_tick_accepts_step(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            p = I.Progress("x", 10, every=5)
            p.tick(10)
        self.assertIn("10/10", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
