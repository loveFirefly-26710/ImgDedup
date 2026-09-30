"""M4 缓存层的测试。

两条最重要的断言：
  1. 解码后端的惰性探测必须线程安全：这里踩过一个静默降级的坑：
     并发下 8/9 个线程看到未初始化的后端，`probe_meta` 返回 None，
     表现是"3201 个文件只解析出 1 个"，感知层整段失效却照样跑完不报错。
  2. 缓存不能改变分组结论：缓存一旦让结果漂移，就是静默的错误结论。
"""

import os
import shutil
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgdedup_core import cache as K  # noqa: E402
from imgdedup_core import decode as D  # noqa: E402
from imgdedup_core import feature as F  # noqa: E402
from imgdedup_core import group as G  # noqa: E402
from imgdedup_core import scan as S  # noqa: E402
from imgdedup_core import scratch as SC  # noqa: E402

BOX = None

#: 真实图库位置。有些用例必须真解码才算数（假路径只会画占位色块），
#: 所以要用真实图片。
#:
#: 图库不在时跳过，不要失败：CI 的干净 runner 上没有这个目录。
#: 早先用 `assertTrue(self.entries, ...)` 硬断言，实测在"图库不存在"时
#: 7 项里崩 5 项，整个 CI 变红。换机器时用环境变量指过去：
#:     set IMGDEDUP_TEST_LIBRARY=D:\我的图库
SRC = os.environ.get("IMGDEDUP_TEST_LIBRARY", r"E:\1\test")
needs_library = unittest.skipUnless(
    os.path.isdir(SRC),
    f"需要真实图库（{SRC} 不存在）—— 用 IMGDEDUP_TEST_LIBRARY 指过去")


def setUpModule():
    global BOX
    BOX = SC.scratch_root() / "_cache_tests"
    shutil.rmtree(BOX, ignore_errors=True)
    BOX.mkdir(parents=True, exist_ok=True)


def tearDownModule():
    shutil.rmtree(BOX, ignore_errors=True)


def _real_images(n=3):
    """从真实图库里取几张（只读），用于需要真解码的用例。"""
    out = []
    for e in S.walk(SRC):
        if e.size > 40_000:
            out.append(e)
        if len(out) >= n:
            break
    return out


class TestBackendProbeThreadSafety(unittest.TestCase):
    """惰性探测的竞态：这是本轮最值钱的一条测试。"""

    def test_concurrent_probe_sees_initialised_backends(self):
        b = D._Backends()
        seen = []

        def go(_i):
            b.probe()
            seen.append(b.pil is not None)

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(go, range(8)))

        self.assertEqual(len(seen), 8)
        self.assertTrue(all(seen),
                        f"并发 probe 后有线程看到未初始化的后端：{seen}")

    def test_probe_is_idempotent(self):
        b = D._Backends()
        b.probe()
        first = b.summary()
        b.probe()
        self.assertEqual(first, b.summary())

    def test_import_errors_are_recorded_not_swallowed(self):
        b = D._Backends()
        b.probe()
        self.assertIsInstance(b.errors, dict)


@needs_library
class TestFingerprintStore(unittest.TestCase):
    def setUp(self):
        self.dir = BOX / "fp"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = str(self.dir / "cache.db")
        self.entries = _real_images(3)
        self.assertTrue(self.entries, "拿不到真实图片，无法测试")
        for e in self.entries:
            m = D.probe_meta(e.path, e.ext)
            if m:
                e.width, e.height = m.width, m.height

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _compute(self, e):
        e.quick = F.quick_hash(e.path)
        e.full = F.full_hash(e.path)
        arr, backend = D.load_small(e.path, 256, e.ext, e.pixels)
        f = F.extract(arr, backend, "")
        e.phash, e.dhash, e.hist = f.phash, f.dhash, f.hist
        e.feat_ok, e.feat_note, e.thumb_backend = f.ok, f.note, backend
        return e

    def test_roundtrip_restores_everything(self):
        for e in self.entries:
            self._compute(e)
        saved = K.FingerprintStore(self.db)
        n = saved.save(self.entries)
        saved.close()
        self.assertEqual(n, len(self.entries))

        fresh = list(S.walk(SRC))[: len(self.entries)]
        # 用同样的文件重新走一遍
        fresh = []
        for e in self.entries:
            st = os.stat(e.path)
            fresh.append(type(e)(path=e.path, size=st.st_size, mtime_ns=st.st_mtime_ns,
                                 ext=e.ext, dev=getattr(st, "st_dev", 0),
                                 ino=getattr(st, "st_ino", 0)))
        store = K.FingerprintStore(self.db)
        store.load()
        hits = store.apply(fresh)
        store.close()
        self.assertEqual(hits, len(fresh))

        for orig, got in zip(self.entries, fresh):
            self.assertEqual(got.quick, orig.quick, "quick_hash 没恢复")
            self.assertEqual(got.full, orig.full, "full_hash 没恢复")
            self.assertEqual(got.width, orig.width, "尺寸没恢复")
            self.assertEqual(got.phash, orig.phash, "pHash 没恢复")
            self.assertEqual(got.dhash, orig.dhash, "dHash 没恢复")
            self.assertEqual(got.feat_ok, orig.feat_ok)
            self.assertTrue(got.feat_cached, "feat_cached 没置位，感知阶段不会被跳过")
            import numpy as np

            np.testing.assert_allclose(np.asarray(got.hist), np.asarray(orig.hist),
                                       rtol=1e-5, atol=1e-7)

    def test_miss_on_changed_mtime(self):
        for e in self.entries:
            self._compute(e)
        store = K.FingerprintStore(self.db)
        store.save(self.entries)
        store.close()

        store = K.FingerprintStore(self.db)
        store.load()
        tampered = list(self.entries)
        tampered[0] = type(tampered[0])(
            path=tampered[0].path, size=tampered[0].size,
            mtime_ns=tampered[0].mtime_ns + 1, ext=tampered[0].ext)
        hits = store.apply(tampered)
        store.close()
        self.assertEqual(hits, len(tampered) - 1, "mtime 变了却仍命中缓存")

    def test_cache_does_not_change_grouping(self):
        """关键：带缓存与不带缓存必须得到同样的分组。"""
        for e in self.entries:
            self._compute(e)
        before = G.perceptual_groups(self.entries)

        store = K.FingerprintStore(self.db)
        store.save(self.entries)
        store.close()

        fresh = []
        for e in self.entries:
            st = os.stat(e.path)
            fresh.append(type(e)(path=e.path, size=st.st_size, mtime_ns=st.st_mtime_ns,
                                 ext=e.ext))
        store = K.FingerprintStore(self.db)
        store.load()
        store.apply(fresh)
        store.close()
        after = G.perceptual_groups(fresh)

        self.assertEqual(len(before), len(after))
        self.assertEqual([len(g) for g in before], [len(g) for g in after])


@needs_library
class TestThumbStore(unittest.TestCase):
    def setUp(self):
        self.dir = BOX / "thumbs"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.entries = _real_images(2)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_put_get_roundtrip(self):
        import numpy as np

        st = K.ThumbStore(str(self.dir))
        rgb = np.zeros((32, 48, 3), dtype=np.uint8)
        rgb[:, :, 0] = 200
        rgb[:, :, 1] = 30
        key = "abcdef0123-256"
        self.assertTrue(st.put(key, rgb))
        got = st.get(key)
        self.assertIsNotNone(got, "写进去又读不出来")
        self.assertEqual(got.shape, (32, 48, 3))
        # WebP 有损，颜色允许有偏差，但通道关系必须对
        self.assertGreater(int(got[:, :, 0].mean()), int(got[:, :, 1].mean()))

    def test_miss_returns_none(self):
        st = K.ThumbStore(str(self.dir))
        self.assertIsNone(st.get("nothing-here-256"))

    def test_thumb_for_decodes_then_hits_cache(self):
        st = K.ThumbStore(str(self.dir))
        e = self.entries[0]
        m = D.probe_meta(e.path, e.ext)
        if m:
            e.width, e.height = m.width, m.height
        a1, src1 = st.thumb_for(e, 256)
        self.assertIsNotNone(a1)
        self.assertNotEqual(src1, "cache")
        a2, src2 = st.thumb_for(e, 256)
        self.assertEqual(src2, "cache", "第二次没有命中磁盘缓存")
        self.assertEqual(a1.shape, a2.shape)

    def test_key_changes_with_mtime(self):
        st = K.ThumbStore(str(self.dir))
        e = self.entries[0]
        k1 = st.key(e.path, 256, e.mtime_ns)
        k2 = st.key(e.path, 256, e.mtime_ns + 1)
        self.assertNotEqual(k1, k2, "mtime 变了 key 必须变，否则会读到过期缩略图")


class TestLargeImageDegradation(unittest.TestCase):
    def test_threshold(self):
        self.assertFalse(K.should_skip_features(10_000_000))
        self.assertTrue(K.should_skip_features(233_000_000))

    def test_zero_limit_disables_degradation(self):
        self.assertFalse(K.should_skip_features(10**12, limit=0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
