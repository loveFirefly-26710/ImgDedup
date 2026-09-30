"""解码层：格式能力探测 + 多级瀑布路由 + 元数据提取 + 缩略图。

设计要点（全部来自本机实测，不是推测）：

1. libvips（pyvips-binary 8.18.6）没有打包 ImageMagick 委托。
   实测 frowny.bmp 是合法 BMP（magic 'BM'、大小字段与实际一致），
   但 libvips 报 "is not a known file format"。PDF 同理。
   => 不能假设 libvips 通吃，必须做多级瀑布。

2. 实测能力矩阵：
   pyvips  : JPEG PNG GIF WebP TIFF SVG  ✅  ／ BMP PSD PDF AI ✗
   Pillow  : BMP PSD ICO AVIF TGA ...    ✅  ／ PDF ✗
   pdfium  : PDF（AI 的 PDF 变体同源）    ✅

3. 一律走 shrink-on-load / 小尺寸缩略图，永不整图解压。
   实测 47MP 图全解码 = 134 MB / 346 ms；256px 缩略图 = 2.2 ms。
"""

from __future__ import annotations

import os
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# libvips 内建线程池与我们的 Python 线程池会互相放大：
# 8 个 Python 线程 × 12 个 libvips 线程 = 96 线程，必然抖动。
# 设为 1，由我们自己的闸门统一控制并发。
os.environ.setdefault("VIPS_CONCURRENCY", "1")

import numpy as np  # noqa: E402


# ================================================================ 后端可用性

class _Backends:
    """解码后端探测（惰性 + 线程安全）。

    这里踩过一个静默降级的坑：
    早先的写法是 `if self._probed: return` 之后立刻 `self._probed = True`，
    再去 import。于是在线程池里并发的 9 个线程中，只有 1 个真去导入，
    其余 8 个立刻返回、看到的 `_pil` 还是 None → `_meta_pillow` 返回 None。
    表现是「3201 个文件只解析出 1 个」，感知层因此整段失效，
    但流程照样跑完、不报任何错。

    修法：只在全部导入完成之后才置位 `_probed`，并且用锁让
    并发调用者等待而不是抢跑（双重检查 + 锁）。
    另外把被吞掉的导入异常记进 `errors`，别再无声无息。
    """

    def __init__(self) -> None:
        self._vips = None
        self._pil = None
        self._pdfium = None
        self._probed = False
        self._lock = threading.Lock()
        self.errors: dict = {}

    def probe(self) -> None:
        if self._probed:
            return
        with self._lock:
            if self._probed:          # 双重检查：等锁期间别人可能已完成
                return
            try:
                import pyvips

                self._vips = pyvips
            except Exception as e:  # noqa: BLE001
                self._vips = None
                self.errors["pyvips"] = f"{type(e).__name__}: {e}"
            try:
                from PIL import Image

                # 实测本图库含 233 MP 的图（12828x18188）和 130 MP 的图，
                # Pillow 默认阈值 89 MP 会抛 DecompressionBombWarning。
                # 我们一律走小尺寸缩略图，且真实尺寸已由元数据阶段掌握，
                # 因此显式关闭该阈值，而不是让它变成噪音。
                Image.MAX_IMAGE_PIXELS = None
                Image.init()
                self._pil = Image
            except Exception as e:  # noqa: BLE001
                self._pil = None
                self.errors["Pillow"] = f"{type(e).__name__}: {e}"
            try:
                import pypdfium2 as pdfium

                self._pdfium = pdfium
            except Exception as e:  # noqa: BLE001
                self._pdfium = None
                self.errors["pypdfium2"] = f"{type(e).__name__}: {e}"
            self._probed = True       # 只在全部完成之后才置位

    @property
    def vips(self):
        self.probe()
        return self._vips

    @property
    def pil(self):
        self.probe()
        return self._pil

    @property
    def pdfium(self):
        self.probe()
        return self._pdfium

    def summary(self) -> dict:
        self.probe()
        out = {}
        if self.errors:
            out["import_errors"] = dict(self.errors)
        if self._vips:
            out["pyvips"] = "%d.%d.%d" % (
                self._vips.version(0), self._vips.version(1), self._vips.version(2))
        if self._pil:
            out["Pillow"] = getattr(self._pil, "__version__", "?")
        if self._pdfium:
            out["pypdfium2"] = _pypdfium2_version()
        return out


def _pypdfium2_version() -> str:
    """pypdfium2 的版本号。

    冻结后 `importlib.metadata` 读不到 dist-info（PyInstaller 默认不带
    metadata），所以要退回它自带的版本模块：那条路读的是包内的
    `version.json`，冻结后照样在。
    """
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("pypdfium2")
    except Exception:  # noqa: BLE001
        pass
    try:
        import pypdfium2_raw.version as _V

        return f"pdfium {_V.PDFIUM_INFO}"
    except Exception:  # noqa: BLE001
        return "?"


BACKENDS = _Backends()


# ================================================================ 格式路由表

VIPS_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp",
             ".tif", ".tiff", ".svg", ".heif", ".heic", ".avif", ".jp2", ".jxl"}
PILLOW_EXTS = {".bmp", ".dib", ".psd", ".psb", ".ico", ".dds", ".tga", ".pcx",
               ".ppm", ".pgm", ".pbm", ".pnm", ".xbm", ".im", ".msp", ".sgi", ".cur"}
PDF_EXTS = {".pdf", ".ai"}   # 由 pypdfium2 处理
HEADER_EXTS = {".eps", ".svgz"}

#: 支持的全部扩展名
SUPPORTED_EXTS = VIPS_EXTS | PILLOW_EXTS | PDF_EXTS | HEADER_EXTS


# ================================================================ 魔数嗅探

_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"8BPS", ".psd"),
    (b"%PDF", ".pdf"),
    (b"\x00\x00\x01\x00", ".ico"),
)


def sniff_format(path: str, head: Optional[bytes] = None) -> str:
    """按魔数嗅探格式。用于处理 E:\\1\\test 里那两个无扩展名的文件。"""
    if head is None:
        try:
            with open(path, "rb") as fh:
                head = fh.read(64)
        except OSError:
            return ""
    if not head:
        return ""
    for sig, ext in _MAGIC:
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return ".heic"
        if brand in (b"avif", b"avis"):
            return ".avif"
    stripped = head.lstrip()[:8].lower()
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<svg"):
        return ".svg"
    return ""


# ================================================================ 元数据

@dataclass
class Meta:
    width: int = 0
    height: int = 0
    bands: int = 0
    fmt: str = ""
    backend: str = ""
    frames: int = 1
    animated: bool = False
    note: str = ""

    @property
    def pixels(self) -> int:
        return self.width * self.height


def _meta_psd(path: str) -> Optional[Meta]:
    """PSD/PSB 头解析：8BPS + 版本 + 通道 + 高 + 宽 + 位深 + 色彩模式。

    这是 PSD 无样本条件下的降级路径：只取尺寸，不解码像素。
    """
    with open(path, "rb") as fh:
        h = fh.read(32)
    if len(h) < 26 or h[:4] != b"8BPS":
        return None
    ver = int.from_bytes(h[4:6], "big")
    channels = int.from_bytes(h[12:14], "big")
    height = int.from_bytes(h[14:18], "big")
    width = int.from_bytes(h[18:22], "big")
    depth = int.from_bytes(h[22:24], "big")
    mode = int.from_bytes(h[24:26], "big")
    modes = {0: "Bitmap", 1: "Grayscale", 2: "Indexed", 3: "RGB",
             4: "CMYK", 7: "Multichannel", 8: "Duotone", 9: "Lab"}
    return Meta(width=width, height=height, bands=channels,
                fmt=f"PSD{ver} {depth}bit {modes.get(mode, mode)}",
                backend="psd-header", note="仅头解析，不支持画面相似度")


def _meta_svg(path: str) -> Optional[Meta]:
    """SVG：解析 width/height/viewBox。注意可能没有固有像素尺寸。"""
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None

    def _num(v):
        if not v:
            return 0
        v = v.strip()
        for suffix in ("px", "pt", "mm", "cm", "in", "%"):
            if v.endswith(suffix):
                v = v[: -len(suffix)]
                break
        try:
            return int(round(float(v)))
        except ValueError:
            return 0

    w = _num(root.get("width"))
    h = _num(root.get("height"))
    note = ""
    if w == 0 or h == 0:
        vb = root.get("viewBox")
        if vb:
            parts = vb.replace(",", " ").split()
            if len(parts) == 4:
                try:
                    w = int(round(float(parts[2])))
                    h = int(round(float(parts[3])))
                    note = "尺寸来自 viewBox，无固有像素尺寸"
                except ValueError:
                    pass
    if w == 0 or h == 0:
        note = "无固有像素尺寸，仅可作矢量比对"
    return Meta(width=w, height=h, bands=4, fmt="SVG", backend="svg-header", note=note)


def _meta_bmp(path: str) -> Optional[Meta]:
    with open(path, "rb") as fh:
        h = fh.read(32)
    if len(h) < 26 or h[:2] != b"BM":
        return None
    width = int.from_bytes(h[18:22], "little", signed=True)
    height = int.from_bytes(h[22:26], "little", signed=True)
    bpp = int.from_bytes(h[28:30], "little") if len(h) >= 30 else 0
    return Meta(width=abs(width), height=abs(height), bands=max(1, bpp // 8),
                fmt=f"BMP {bpp}bit", backend="bmp-header")


def _meta_eps(path: str) -> Optional[Meta]:
    """EPS（含 AI 的 EPS 变体）：从头部找 %%BoundingBox。

    只取尺寸，无法解码像素——这是 AI 在无 Ghostscript 时的唯一可用信息。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if b"%!PS" not in head[:64] and b"%!PS" not in head:
        return None
    for key in (b"%%BoundingBox:", b"%%HiResBoundingBox:"):
        i = head.find(key)
        if i < 0:
            continue
        line = head[i: head.find(b"\n", i) if head.find(b"\n", i) > 0 else i + 128]
        nums = []
        for tok in line[len(key):].split():
            try:
                nums.append(float(tok))
            except ValueError:
                break
            if len(nums) == 4:
                break
        if len(nums) == 4:
            w = int(round(abs(nums[2] - nums[0])))
            h = int(round(abs(nums[3] - nums[1])))
            return Meta(width=w, height=h, bands=4, fmt="EPS/AI",
                        backend="eps-header", note="仅 BoundingBox，不支持画面相似度")
    return None


def _meta_pillow(path: str) -> Optional[Meta]:
    Image = BACKENDS.pil
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            n = getattr(im, "n_frames", 1)
            bands = len(im.getbands())
            return Meta(width=im.width, height=im.height, bands=bands,
                        fmt=im.format or "", backend="pillow",
                        frames=n, animated=n > 1)
    except Exception:
        return None


def _meta_vips(path: str) -> Optional[Meta]:
    vips = BACKENDS.vips
    if vips is None:
        return None
    try:
        im = vips.Image.new_from_file(path, access="sequential")
        n = 1
        try:
            n = im.get("n-pages") or 1
        except Exception:
            pass
        return Meta(width=im.width, height=im.height, bands=im.bands,
                    fmt=im.format or "", backend="pyvips",
                    frames=int(n), animated=int(n) > 1)
    except Exception:
        return None


def _meta_pdfium(path: str) -> Optional[Meta]:
    pdfium = BACKENDS.pdfium
    if pdfium is None:
        return None
    doc = None
    try:
        doc = pdfium.PdfDocument(path)
        n = len(doc)
        if n <= 0:
            return None
        w, h = doc[0].get_size()
        return Meta(width=int(round(w)), height=int(round(h)), bands=3,
                    fmt="PDF/AI", backend="pdfium", frames=n,
                    animated=n > 1)
    except Exception:
        return None
    finally:
        if doc is not None:
            doc.close()


#: 每种扩展名的元数据提取顺序。便宜的、无副作用的排前面。
_META_PLAN = {
    ".svg": (_meta_svg, _meta_vips),
    ".svgz": (_meta_svg,),
    ".psd": (_meta_psd, _meta_pillow),
    ".psb": (_meta_psd, _meta_pillow),
    ".bmp": (_meta_bmp, _meta_pillow),
    ".dib": (_meta_bmp, _meta_pillow),
    ".eps": (_meta_eps,),
    ".ai": (_meta_pdfium, _meta_eps),
    ".pdf": (_meta_pdfium,),
}
_META_DEFAULT = (_meta_pillow, _meta_vips)


def probe_meta(path: str, ext: str = "") -> Optional[Meta]:
    """提取元数据。只读文件头，不解码像素。"""
    ext = (ext or os.path.splitext(path)[1]).lower()
    if not ext:
        ext = sniff_format(path)
    plan = _META_PLAN.get(ext, _META_DEFAULT)
    for fn in plan:
        m = fn(path)
        if m and m.width > 0 and m.height > 0:
            if not m.fmt:
                m.fmt = ext.lstrip(".").upper()
            return m
    return None


# ================================================================ 缩略图

def _vips_to_rgb(arr: np.ndarray) -> np.ndarray:
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] == 1:
        arr = np.repeat(arr, 3, axis=2)
    elif arr.ndim == 3 and arr.shape[2] == 2:
        arr = np.stack([arr[:, :, 0]] * 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] >= 4:
        arr = arr[:, :, :3]
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


def _thumb_vips(path: str, size: int) -> Optional[np.ndarray]:
    vips = BACKENDS.vips
    if vips is None:
        return None
    try:
        im = vips.Image.thumbnail(path, size, height=size)
        if im.bands > 3:
            im = im.extract_band(0, n=3)
        elif im.bands == 1:
            im = im.bandjoin([im, im])
        return _vips_to_rgb(im.numpy())
    except Exception:
        return None


def _thumb_pillow(path: str, size: int) -> Optional[np.ndarray]:
    Image = BACKENDS.pil
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            try:
                im.draft("RGB", (size, size))
            except Exception:
                pass
            im = im.convert("RGB")
            im.thumbnail((size, size), Image.LANCZOS)
            return np.asarray(im, dtype=np.uint8)
    except Exception:
        return None


def _thumb_pdfium(path: str, size: int) -> Optional[np.ndarray]:
    pdfium = BACKENDS.pdfium
    if pdfium is None:
        return None
    doc = None
    try:
        doc = pdfium.PdfDocument(path)
        if len(doc) <= 0:
            return None
        page = doc[0]
        w, h = page.get_size()
        longest = max(w, h)
        scale = size / longest if longest > 0 else 1.0
        scale = max(0.05, min(scale, 4.0))
        bitmap = page.render(scale=scale)
        try:
            # to_numpy() 直接给 (h, w, 3) uint8：灰度页也是 3 通道，
            # 所以不需要再走 _vips_to_rgb 那条单通道展开的路。
            arr = np.ascontiguousarray(bitmap.to_numpy())
        finally:
            bitmap.close()
        return arr
    except Exception:
        return None
    finally:
        if doc is not None:
            doc.close()


_THUMB_PLAN = {
    ".bmp": (_thumb_pillow, _thumb_vips),
    ".dib": (_thumb_pillow,),
    ".psd": (_thumb_pillow,),
    ".psb": (_thumb_pillow,),
    ".ico": (_thumb_pillow,),
    ".cur": (_thumb_pillow,),
    ".dds": (_thumb_pillow,),
    ".tga": (_thumb_pillow,),
    ".pcx": (_thumb_pillow,),
    ".pdf": (_thumb_pdfium,),
    ".ai": (_thumb_pdfium,),
    # Pillow 无加载器或效果差，必须走 libvips
    ".svg": (_thumb_vips,),
    ".heif": (_thumb_vips,),
    ".heic": (_thumb_vips,),
    ".avif": (_thumb_vips,),
    ".jp2": (_thumb_vips,),
    ".jxl": (_thumb_vips,),
}

#: 默认顺序：Pillow 优先。
#: 实测 240 张随机样本：Pillow 中位 14.9 ms vs pyvips 23.3 ms，小图档差距最大
#: （<0.5 MP：5.4 ms vs 16.3 ms，3.0 倍）。原因是 pyvips 每次调用有约 11 ms
#: 的固定开销——同一张 358x384 小图重复调用 50 次仍是 12 ms/次，
#: 与 IO 和像素量都无关。
_THUMB_DEFAULT = (_thumb_pillow, _thumb_vips)

#: 超大 PNG/TIFF 反过来：libvips 能跳过行解码，Pillow 只能整图解码。
_THUMB_ROW_SKIP = (_thumb_vips, _thumb_pillow)

LARGE_PIXELS = 8_000_000
ROW_SKIP_EXTS = {".png", ".tif", ".tiff"}


def load_small(path: str, size: int = 256, ext: str = "", pixels: int = 0):
    """按瀑布加载小尺寸缩略图。返回 (RGB 数组 或 None, 实际使用的后端)。

    路由策略完全由实测决定，不是拍脑袋：

      * 默认 Pillow 优先——整体快 1.5 倍，小图快 3 倍。
      * 超大 PNG/TIFF 改走 pyvips——libvips 的 shrink-on-load 能跳过行。
      * JPEG 两边都慢：DCT 缩放只省了 IDCT 和上采样，熵解码必须解完
        全部系数，成本仍与原图像素数成正比。实测 233 MP 的 JPEG 缩略图
        要 210 ms（Pillow）/ 210 ms（pyvips），这是格式本身的下限。
    """
    ext = (ext or os.path.splitext(path)[1]).lower()
    if not ext:
        ext = sniff_format(path)
    if ext in _THUMB_PLAN:
        plan = _THUMB_PLAN[ext]
    elif pixels >= LARGE_PIXELS and ext in ROW_SKIP_EXTS:
        plan = _THUMB_ROW_SKIP
    else:
        plan = _THUMB_DEFAULT
    for fn in plan:
        arr = fn(path, size)
        if arr is not None and arr.size > 0:
            return arr, fn.__name__.replace("_thumb_", "")
    return None, "none"


def estimate_decode_bytes(meta: Meta, target: int = 256) -> int:
    """估算解码某图所需的内存（按缩略图尺寸，而不是原图）。

    这正是 shrink-on-load 的价值：8K 图按目标尺寸反算后只需约 0.2 MB，
    而不是 134 MB。
    """
    if meta.width <= 0 or meta.height <= 0:
        return 1024 * 1024
    scale = max(1.0, max(meta.width, meta.height) / max(1, target))
    w = max(1, int(meta.width / scale))
    h = max(1, int(meta.height / scale))
    return max(64 * 1024, w * h * 3 * 4)  # 4 字节/通道，容纳中间浮点缓冲


# ================================================================ 动图
#
# 预览框要"动起来"，所以这里提供逐帧读取。三条硬约束：
#
# 1. 只用 Pillow。 libvips 也能逐页读 GIF/WebP，但它的 `page-height`
#    语义在各版本间不稳定，而且我们要的是"每帧自己的显示时长"，
#    libvips 的 `delay` 元数据在 WebP 上常缺失。Pillow 的 `im.info["duration"]`
#    是毫秒、`seek()` 语义清晰，实测最可靠。
# 2. 必须设上限。 动图是唯一能"合法地把内存吃光"的格式：
#    一张 800×800 的 GIF 有 2000 帧就是 3.8 GB（RGB）。所以帧数、
#    单帧边长、总字节三者都要封顶，超了就截断播放而不是拒绝加载。
#    用户至少能看到前几秒，比"加载失败"友好。
# 3. 必须先算缩放再解帧。 每帧 `thumbnail()` 一次是最省的：
#    Pillow 解 GIF/WebP 时按需解码，配合 resize 不会把原始大图留在内存。

#: 单帧最长边（预览框只有几百像素宽，512 够放大到 2× 还不糊）
ANIM_FRAME_SIZE = 512
#: 最多播多少帧。超出截断：防"合法地吃光内存"。
ANIM_MAX_FRAMES = 300
#: 全部帧加起来最多多少字节（RGB 估算）。约 300 MB。
ANIM_MAX_BYTES = 300 * 1024 * 1024
#: 没有 duration 字段时的兜底帧时长（毫秒）。GIF 规范默认 100ms。
ANIM_DEFAULT_MS = 100
#: 单帧时长的合法区间。低于 20ms 肉眼无意义还狂占 CPU；高于 10s 是
#: 制作者笔误（有人把秒当毫秒写），夹住避免"看着像卡死"。
ANIM_MIN_MS = 20
ANIM_MAX_MS = 10_000


@dataclass
class Animation:
    """一段动图的解码结果。"""

    frames: list = field(default_factory=list)   #: list[np.ndarray]，RGB
    delays: list = field(default_factory=list)   #: 每帧时长（毫秒），与 frames 等长
    truncated: bool = False                      #: 是否因超限被截断
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.frames)

    @property
    def total_ms(self) -> int:
        return int(sum(self.delays))


def load_animation(path: str, max_side: int = ANIM_FRAME_SIZE,
                   max_frames: int = ANIM_MAX_FRAMES,
                   max_bytes: int = ANIM_MAX_BYTES) -> Optional[Animation]:
    """读动图的每一帧。静态图或失败都返回 `None`。

    调用方（预览框）要在后台线程里调它：一帧几百毫秒、
    几十帧就是秒级，同步调会让界面卡死。
    """
    Image = BACKENDS.pil
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            n = getattr(im, "n_frames", 1)
            if n <= 1:
                return None                     # 静态图，交给普通缩略图路径
            frames: list = []
            delays: list = []
            used = 0
            truncated = False
            for i in range(n):
                if i >= max_frames or used >= max_bytes:
                    truncated = True
                    break
                try:
                    im.seek(i)
                except EOFError:
                    break
                try:
                    fr = im.convert("RGB")
                except Exception:
                    continue
                # 先缩后算：缩完再估内存，避免按原尺寸误判超限。
                fr.thumbnail((max_side, max_side), Image.LANCZOS)
                arr = np.asarray(fr, dtype=np.uint8)
                need = int(arr.nbytes)
                if frames and used + need > max_bytes:
                    truncated = True
                    break
                used += need
                frames.append(np.ascontiguousarray(arr))
                # GIF 的 duration 单位是毫秒（Pillow 已换算）；
                # 少数文件缺失或为 0 → 用兜底值。
                ms = int(im.info.get("duration") or 0)
                if ms <= 0:
                    ms = ANIM_DEFAULT_MS
                delays.append(max(ANIM_MIN_MS, min(ANIM_MAX_MS, ms)))
            if not frames:
                return None
            note = ""
            if truncated:
                note = (f"帧数过多，只播前 {len(frames)} 帧"
                        f"（原 {n} 帧）")
            return Animation(frames=frames, delays=delays,
                             truncated=truncated, note=note)
    except Exception:
        return None


def is_animated(path: str, ext: str = "", meta: Optional["Meta"] = None) -> bool:
    """快速判断是不是动图（不解码像素）。

    优先用已探好的 `meta.animated`；没有再按扩展名兜底 ——
    `.gif` 不一定是动图（单帧 GIF 很常见），所以扩展名只能当"值得一试"。
    """
    if meta is not None and meta.animated:
        return True
    if meta is not None and meta.frames > 1:
        return True
    e = (ext or os.path.splitext(path)[1]).lower()
    # 只有这三种格式支持多帧。`.png` 可能是 APNG，但探测成本高，
    # 交给 `load_animation` 用 n_frames 判定即可。
    return e in (".gif", ".webp", ".apng")
