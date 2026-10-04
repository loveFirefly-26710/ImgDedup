"""`imgdedup_core/decode.py`：魔数嗅探、元数据探测、缩略图路由、动图。

用 Pillow 现场生成小图，不依赖仓库里的任何素材，也不联网。
"""

import os
import tempfile
import threading
import unittest

from PIL import Image

from imgdedup_core import decode as D


def _save(name: str, size=(64, 48), color=(255, 0, 0), fmt=None) -> str:
    path = os.path.join(_TMP.name, name)
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


_TMP = None


def _write_bytes(name: str, data: bytes) -> str:
    path = os.path.join(_TMP.name, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def setUpModule():
    global _TMP
    _TMP = tempfile.TemporaryDirectory(prefix="imgdedup_decode_")


def tearDownModule():
    _TMP.cleanup()


class TestSniffFormat(unittest.TestCase):

    def test_real_files(self):
        self.assertEqual(D.sniff_format(_save("a.png")), ".png")
        self.assertEqual(D.sniff_format(_save("a.jpg")), ".jpg")
        self.assertEqual(D.sniff_format(_save("a.bmp")), ".bmp")
        self.assertEqual(D.sniff_format(_save("a.gif")), ".gif")

    def test_synthetic_headers(self):
        cases = [
            (b"8BPS" + b"\x00" * 30, ".psd"),
            (b"%PDF-1.7", ".pdf"),
            (b"II*\x00" + b"\x00" * 10, ".tif"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp"),
            (b"<?xml version='1.0'?><svg/>", ".svg"),
            (b"<svg xmlns='...'></svg>", ".svg"),
        ]
        for head, want in cases:
            with self.subTest(want=want):
                self.assertEqual(D.sniff_format("ignored", head=head), want)

    def test_unknown_and_missing(self):
        self.assertEqual(D.sniff_format("ignored", head=b"nonsense"), "")
        self.assertEqual(D.sniff_format("ignored", head=b""), "")
        self.assertEqual(D.sniff_format(os.path.join(_TMP.name, "nope.png")), "")


class TestMeta(unittest.TestCase):

    def test_pixels(self):
        self.assertEqual(D.Meta(width=30, height=40).pixels, 1200)

    def test_probe_png(self):
        m = D.probe_meta(_save("m.png", size=(64, 48)))
        self.assertIsNotNone(m)
        self.assertEqual((m.width, m.height), (64, 48))
        self.assertEqual(m.fmt, "PNG")
        self.assertEqual(m.bands, 3)
        self.assertEqual(m.backend, "pillow")

    def test_probe_bmp(self):
        m = D.probe_meta(_save("m.bmp", size=(20, 10), fmt="BMP"))
        self.assertIsNotNone(m)
        self.assertEqual((m.width, m.height), (20, 10))

    def test_probe_psd_reads_the_header_without_pillow(self):
        hdr = bytearray(32)
        hdr[0:4] = b"8BPS"
        hdr[4:6] = (1).to_bytes(2, "big")
        hdr[12:14] = (3).to_bytes(2, "big")     # channels
        hdr[14:18] = (40).to_bytes(4, "big")    # height
        hdr[18:22] = (30).to_bytes(4, "big")    # width
        hdr[22:24] = (8).to_bytes(2, "big")     # depth
        hdr[24:26] = (3).to_bytes(2, "big")     # RGB
        path = _write_bytes("hdr.psd", bytes(hdr))
        m = D.probe_meta(path, ".psd")
        self.assertIsNotNone(m)
        self.assertEqual((m.width, m.height, m.bands), (30, 40, 3))
        self.assertEqual(m.backend, "psd-header")

    def test_probe_garbage_returns_none(self):
        path = _write_bytes("junk.png", b"not an image at all")
        self.assertIsNone(D.probe_meta(path, ".png"))


class TestPlans(unittest.TestCase):

    def test_meta_plan_covers_the_awkward_formats(self):
        for ext in (".svg", ".svgz", ".psd", ".psb", ".bmp", ".dib",
                    ".eps", ".ai", ".pdf"):
            with self.subTest(ext=ext):
                self.assertIn(ext, D._META_PLAN)

    def test_thumb_plan_targets_pdfium_for_pdf(self):
        self.assertEqual(D._THUMB_PLAN[".pdf"][0].__name__, "_thumb_pdfium")

    def test_large_pixel_threshold(self):
        self.assertEqual(D.LARGE_PIXELS, 8_000_000)
        self.assertEqual(D.ROW_SKIP_EXTS, {".png", ".tif", ".tiff"})


class TestEstimateDecodeBytes(unittest.TestCase):

    def test_unknown_size_falls_back_to_one_mib(self):
        self.assertEqual(D.estimate_decode_bytes(D.Meta()), 1024 * 1024)

    def test_downscales_to_the_target(self):
        # 1000×1000 缩到 256×256：256*256*3*4
        self.assertEqual(D.estimate_decode_bytes(D.Meta(width=1000, height=1000), 256),
                         256 * 256 * 3 * 4)

    def test_small_images_are_not_upscaled(self):
        self.assertEqual(D.estimate_decode_bytes(D.Meta(width=100, height=100), 256),
                         100 * 100 * 3 * 4)

    def test_never_below_64_kib(self):
        self.assertEqual(D.estimate_decode_bytes(D.Meta(width=1, height=1)), 64 * 1024)

    def test_is_monotonic_below_the_target(self):
        small = D.estimate_decode_bytes(D.Meta(width=100, height=100), 256)
        big = D.estimate_decode_bytes(D.Meta(width=200, height=200), 256)
        self.assertLess(small, big)

    def test_is_capped_at_the_target(self):
        # 缩到 target 之后就不再增长了 —— 这正是「按预计解码字节限流」的前提
        self.assertEqual(D.estimate_decode_bytes(D.Meta(width=5000, height=5000)),
                         D.estimate_decode_bytes(D.Meta(width=8000, height=8000)))


class TestIsAnimated(unittest.TestCase):

    def test_meta_flags_win(self):
        self.assertTrue(D.is_animated("a.png", ".png",
                                      D.Meta(animated=True)))
        self.assertTrue(D.is_animated("a.png", ".png", D.Meta(frames=3)))

    def test_extension_fallback(self):
        for ext in (".gif", ".webp", ".apng"):
            with self.subTest(ext=ext):
                self.assertTrue(D.is_animated(f"a{ext}", ext))
        self.assertFalse(D.is_animated("a.png", ".png", D.Meta()))

    def test_extension_is_read_from_the_path_when_omitted(self):
        self.assertTrue(D.is_animated("x/y/z.GIF"))


class TestLoadAnimation(unittest.TestCase):

    def _gif(self, frames=3) -> str:
        path = os.path.join(_TMP.name, f"anim{frames}.gif")
        imgs = [Image.new("RGB", (32, 24), (i * 60, 0, 0)) for i in range(frames)]
        imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=50,
                     loop=0)
        return path

    def test_multi_frame_gif(self):
        a = D.load_animation(self._gif(3))
        self.assertIsNotNone(a)
        self.assertEqual(len(a.frames), 3)
        self.assertEqual(len(a.delays), 3)
        self.assertFalse(a.truncated)
        self.assertEqual(a.frames[0].ndim, 3)

    def test_single_frame_is_not_an_animation(self):
        self.assertIsNone(D.load_animation(_save("still.gif")))

    def test_max_frames_truncates_with_a_note(self):
        a = D.load_animation(self._gif(4), max_frames=2)
        self.assertIsNotNone(a)
        self.assertEqual(len(a.frames), 2)
        self.assertTrue(a.truncated)
        self.assertIn("2", a.note)

    def test_delays_are_kept_within_bounds(self):
        # 不构造超范围 duration：GIF 的延时字段是 16 位厘秒，99999 这种值
        # 写进去本身就存不下来。这里验的是「读出来的值落在契约区间里」。
        a = D.load_animation(self._gif(3))
        self.assertIsNotNone(a)
        self.assertTrue(all(isinstance(d, int) for d in a.delays))
        self.assertTrue(all(D.ANIM_MIN_MS <= d <= D.ANIM_MAX_MS for d in a.delays))
        self.assertEqual(a.delays[0], 50)

    def test_missing_file_returns_none(self):
        self.assertIsNone(D.load_animation(os.path.join(_TMP.name, "nope.gif")))


class TestLoadSmall(unittest.TestCase):

    def test_returns_rgb_array(self):
        arr, backend = D.load_small(_save("s.png", size=(64, 48)))
        self.assertIsNotNone(arr)
        self.assertEqual(arr.shape, (48, 64, 3))
        self.assertTrue(backend)

    def test_respects_the_target_size(self):
        arr, _b = D.load_small(_save("big.png", size=(600, 400)), 100)
        self.assertIsNotNone(arr)
        self.assertLessEqual(max(arr.shape[:2]), 100)

    def test_extension_is_sniffed_when_missing(self):
        path = os.path.join(_TMP.name, "noext")
        Image.new("RGB", (16, 16)).save(path, format="PNG")
        arr, _b = D.load_small(path, 32)
        self.assertIsNotNone(arr)

    def test_missing_file_returns_none(self):
        arr, backend = D.load_small(os.path.join(_TMP.name, "nope.png"))
        self.assertIsNone(arr)
        self.assertEqual(backend, "none")


class TestBackends(unittest.TestCase):

    def test_probe_is_idempotent_and_reports_pillow(self):
        b = D._Backends()
        self.assertFalse(b._probed)
        b.probe()
        self.assertTrue(b._probed)
        first = b.errors.copy()
        b.probe()
        self.assertEqual(b.errors, first)

    def test_probe_from_many_threads_is_safe(self):
        """探测标志位必须在全部导入完成之后才置位。

        置位早了，并发的线程会看到 `_pil = None`，于是「几千个文件只解析出
        一个」—— 这个 bug 在真实扫描里表现为随机丢文件。
        """
        b = D._Backends()
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(b.pil is not None)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 8)
        self.assertTrue(all(results), "有线程在探测完成前读到了 None")

    def test_module_level_backends_summary(self):
        s = D.BACKENDS.summary()
        self.assertIn("Pillow", s)
        self.assertTrue(s["Pillow"])


if __name__ == "__main__":
    unittest.main()
