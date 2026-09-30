"""PDF / AI 解码后端：pypdfium2（随包）。

## 为什么是它

pypdfium2 底层是 PDFium（Chrome 的 PDF 引擎），许可宽松
（BSD-3-Clause / Apache-2.0），体积 8 MB，所以直接打进包：
PDF / AI 开箱可用，外置组件保持在两个。

许可这一条是硬约束：本项目按 MIT 分发，产物里不能混进强 copyleft
（AGPL）的组件，否则整个组合作品都得跟着改许可。

## 这块最容易悄悄退化的地方

打包。`pypdfium2_raw/bindings.py` 是按 `Path(__file__).parent / "pdfium.dll"`
找 DLL 的，而 PyInstaller 没有它的 hook：少收一个文件，
表现是"源码里好好的，冻结后一 import 就炸"。所以：

  · `module_collection_mode` 让包以真实 .py 文件落地（不能只进 PYZ）；
  · `collect_data_files` 显式收那个 6.9 MB 的 dll。

这两条都有断言钉着（见 TestSpecBundlesPdfium）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJ))

DECODE = PROJ / "imgdedup_core" / "decode.py"
SPEC = PROJ / "imgdedup.spec"
REQ = PROJ / "requirements.txt"
REQ_DEV = PROJ / "requirements-dev.txt"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def minimal_pdf(pages: int = 1) -> bytes:
    """手写一个最小可用的 PDF，当测试夹具。

    刻意不用第三方库造夹具：那会引入"用被测对象测自己"的循环，
    而且会让测试依赖一个我们正想摆脱的库。PDFium 能容忍缺 xref 表
    （它自己重建），所以不用算偏移量。
    """
    objs: list[str] = []
    kids = " ".join(f"{3 + i * 3} 0 R" for i in range(pages))
    objs.append("<</Type/Catalog/Pages 2 0 R>>")
    objs.append(f"<</Type/Pages/Kids[{kids}]/Count {pages}>>")
    font_num = 3 + pages * 3
    for i in range(pages):
        page_num = 3 + i * 3
        content_num = page_num + 1
        w, h = (595, 842) if i % 2 == 0 else (842, 595)
        objs.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {w} {h}]"
            f"/Resources<</Font<</F1 {font_num} 0 R>>>>/Contents {content_num} 0 R>>")
        body = b"BT /F1 24 Tf 72 700 Td (Hello PDFium) Tj ET\n"
        objs.append(f"<</Length {len(body)}>>stream\n{body.decode()}endstream\n")
    objs.append("<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    out = [b"%PDF-1.4\n"]
    for i, body in enumerate(objs, 1):
        out.append(f"{i} 0 obj\n{body}\nendobj\n".encode())
    out.append(f"trailer<</Root 1 0 R/Size {len(objs) + 1}>>\n".encode())
    return b"".join(out)


class TestPdfiumIsTheBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pdf = Path(self.tmp.name) / "doc.pdf"
        self.pdf.write_bytes(minimal_pdf(1))
        self.ai = Path(self.tmp.name) / "doc.ai"
        self.ai.write_bytes(minimal_pdf(1))

    def test_backend_is_loaded(self):
        from imgdedup_core import decode as D

        self.assertIsNotNone(D.BACKENDS.pdfium,
                             "pypdfium2 没加载起来 —— 它是随包依赖，不该缺")
        self.assertEqual(D.BACKENDS.errors, {})

    def test_summary_reports_the_backend(self):
        """自检读的就是 `BACKENDS.summary()`，它必须报出 pypdfium2 版本。

        打包漏了 `pdfium.dll` 时的表现是"import 看着没问题、
        一 render 就炸"，所以自检里不光 import，还真建一次文档。
        """
        from imgdedup_core import decode as D

        self.assertIn("pypdfium2", D.BACKENDS.summary())

    def test_selftest_really_builds_a_document(self):
        """源码断言：自检不能只 import 就放过。"""
        src = read(PROJ / "imgdedup_ui" / "__main__.py")
        self.assertIn("PdfDocument.new()", src,
                      "自检没有真建一次文档 —— 漏收 dll 时它发现不了")

    def test_meta_from_real_pdf(self):
        from imgdedup_core import decode as D

        m = D.probe_meta(str(self.pdf), ".pdf")
        self.assertIsNotNone(m)
        self.assertEqual((m.width, m.height), (595, 842))
        self.assertEqual(m.backend, "pdfium")
        self.assertEqual(m.frames, 1)
        self.assertFalse(m.animated)

    def test_multi_page_reports_frames(self):
        from imgdedup_core import decode as D

        p2 = Path(self.tmp.name) / "two.pdf"
        p2.write_bytes(minimal_pdf(2))
        m = D.probe_meta(str(p2), ".pdf")
        self.assertIsNotNone(m)
        self.assertEqual(m.frames, 2)
        self.assertTrue(m.animated, "多页 PDF 要标成 animated（按帧处理）")

    def test_thumbnail_really_renders(self):
        """判据是真渲染出内容，不是"函数没抛异常"。

        `load_small` 返回 `(数组 或 None, 实际后端名)` 元组：不是裸数组。
        """
        import numpy as np

        from imgdedup_core import decode as D

        arr, backend = D.load_small(str(self.pdf), size=256, ext=".pdf")
        self.assertEqual(backend, "pdfium", "PDF 没走 pdfium 后端")
        self.assertIsNotNone(arr)
        a = np.asarray(arr)
        self.assertEqual(a.ndim, 3)
        self.assertEqual(a.shape[2], 3, "必须是 3 通道 RGB")
        self.assertEqual(a.dtype, np.uint8)
        # 非白像素比例 > 0 才说明真渲染了内容（夹具里有一行文字）
        ink = float((a.sum(axis=2) < 720).mean())
        self.assertGreater(ink, 0.0, "渲染出来是纯白 —— 内容没画上")

    def test_ai_extension_uses_same_backend(self):
        """现代 .ai 本身就是 PDF 兼容格式，同一个后端就能读。"""
        from imgdedup_core import decode as D

        m = D.probe_meta(str(self.ai), ".ai")
        self.assertIsNotNone(m)
        self.assertEqual(m.backend, "pdfium")

    def test_broken_and_missing_files_degrade(self):
        """坏文件 / 不存在的文件只能返回 None，不能把扫描搞崩。"""
        from imgdedup_core import decode as D

        bad = Path(self.tmp.name) / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4\nthis is not a real pdf")
        self.assertIsNone(D.probe_meta(str(bad), ".pdf"))
        self.assertIsNone(D.probe_meta(str(Path(self.tmp.name) / "nope.pdf"), ".pdf"))
        arr, backend = D.load_small(str(bad), size=256, ext=".pdf")
        self.assertIsNone(arr)

    def test_pdf_exts_are_supported(self):
        from imgdedup_core import decode as D

        self.assertIn(".pdf", D.PDF_EXTS)
        self.assertIn(".ai", D.PDF_EXTS)
        self.assertTrue(D.PDF_EXTS <= D.SUPPORTED_EXTS)


class TestNoAgplLeft(unittest.TestCase):
    """许可红线：运行依赖与打包配置里不许出现 AGPL 组件。"""

    def test_no_pymupdf_in_runtime_requirements(self):
        offenders = []
        for line in read(REQ).splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "pymupdf" in s.lower():
                offenders.append(s)
        self.assertEqual(offenders, [], f"运行依赖里出现了 AGPL 组件：{offenders}")

    def test_no_pymupdf_anywhere_in_deps_or_spec(self):
        for p in (REQ, REQ_DEV, SPEC):
            src = read(p)
            self.assertNotIn('"pymupdf"', src, f"{p} 里还有 pymupdf")
            self.assertNotIn('"fitz"', src, f"{p} 里还有 fitz")
            self.assertNotIn("PyMuPDF==", src, f"{p} 里还钉着 PyMuPDF")

    def test_pypdfium2_is_pinned(self):
        self.assertIn("pypdfium2==", read(REQ),
                      "pypdfium2 是随包的解码后端，必须钉在运行依赖里")

    def test_decode_has_no_pymupdf_reference(self):
        src = read(DECODE)
        self.assertNotIn("pymupdf", src.lower())
        self.assertNotIn("fitz", src.lower())


class TestSpecBundlesPdfium(unittest.TestCase):
    """打包配置：少了任何一条，冻结后都会 import 失败。"""

    def test_collects_the_dll(self):
        """PyInstaller 没有 pypdfium2 的 hook，dll 是包内数据文件，
        静态分析不会收：必须显式 collect。"""
        src = read(SPEC)
        self.assertIn('collect_data_files("pypdfium2_raw")', src)

    def test_collects_module_as_source(self):
        """`bindings.py` 按 `Path(__file__).parent / "pdfium.dll"` 找 dll。
        模块若只进 PYZ 归档，`__file__` 指向归档内部 → 找不到。"""
        src = read(SPEC)
        self.assertIn("module_collection_mode", src)
        self.assertIn('"pypdfium2_raw": "py"', src)

    def test_spec_does_not_exclude_pdfium(self):
        src = read(SPEC)
        self.assertNotIn('"pymupdf"', src)
        self.assertNotIn('"_mupdf"', src)


if __name__ == "__main__":
    unittest.main()
