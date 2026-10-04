"""解码与元数据探测。

按扩展名派发到具体后端（pyvips / Pillow / pypdfium2 / 纯文件头解析）。后端是否可用
在首次使用时惰性探测并缓存（`_Backends`），所以「没装 pyvips」不会让导入失败，
只会让相关格式走兜底路径。"""

from __future__ import annotations

import os
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

os.environ.setdefault("VIPS_CONCURRENCY", "1")

import numpy as np



class _Backends:

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
            if self._probed:
                return
            try:
                import pyvips

                self._vips = pyvips
            except Exception as e:
                self._vips = None
                self.errors["pyvips"] = f"{type(e).__name__}: {e}"
            try:
                from PIL import Image

                Image.MAX_IMAGE_PIXELS = None
                Image.init()
                self._pil = Image
            except Exception as e:
                self._pil = None
                self.errors["Pillow"] = f"{type(e).__name__}: {e}"
            try:
                import pypdfium2 as pdfium

                self._pdfium = pdfium
            except Exception as e:
                self._pdfium = None
                self.errors["pypdfium2"] = f"{type(e).__name__}: {e}"
            self._probed = True

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
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("pypdfium2")
    except Exception:
        pass
    try:
        import pypdfium2_raw.version as _V

        return f"pdfium {_V.PDFIUM_INFO}"
    except Exception:
        return "?"


BACKENDS = _Backends()



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
    ".svg": (_thumb_vips,),
    ".heif": (_thumb_vips,),
    ".heic": (_thumb_vips,),
    ".avif": (_thumb_vips,),
    ".jp2": (_thumb_vips,),
    ".jxl": (_thumb_vips,),
}

_THUMB_DEFAULT = (_thumb_pillow, _thumb_vips)

_THUMB_ROW_SKIP = (_thumb_vips, _thumb_pillow)

LARGE_PIXELS = 8_000_000
ROW_SKIP_EXTS = {".png", ".tif", ".tiff"}


def load_small(path: str, size: int = 256, ext: str = "", pixels: int = 0):
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
    if meta.width <= 0 or meta.height <= 0:
        return 1024 * 1024
    scale = max(1.0, max(meta.width, meta.height) / max(1, target))
    w = max(1, int(meta.width / scale))
    h = max(1, int(meta.height / scale))
    return max(64 * 1024, w * h * 3 * 4)



ANIM_FRAME_SIZE = 512
ANIM_MAX_FRAMES = 300
ANIM_MAX_BYTES = 300 * 1024 * 1024
ANIM_DEFAULT_MS = 100
ANIM_MIN_MS = 20
ANIM_MAX_MS = 10_000


@dataclass
class Animation:

    frames: list = field(default_factory=list)
    delays: list = field(default_factory=list)
    truncated: bool = False
    note: str = ""


def load_animation(path: str, max_side: int = ANIM_FRAME_SIZE,
                   max_frames: int = ANIM_MAX_FRAMES,
                   max_bytes: int = ANIM_MAX_BYTES) -> Optional[Animation]:
    Image = BACKENDS.pil
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            n = getattr(im, "n_frames", 1)
            if n <= 1:
                return None
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
                fr.thumbnail((max_side, max_side), Image.LANCZOS)
                arr = np.asarray(fr, dtype=np.uint8)
                need = int(arr.nbytes)
                if frames and used + need > max_bytes:
                    truncated = True
                    break
                used += need
                frames.append(np.ascontiguousarray(arr))
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
    if meta is not None and meta.animated:
        return True
    if meta is not None and meta.frames > 1:
        return True
    e = (ext or os.path.splitext(path)[1]).lower()
    return e in (".gif", ".webp", ".apng")
