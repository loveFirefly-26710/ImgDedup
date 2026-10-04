"""CLIP 语义向量：模型落盘位置、下载、编码与余弦聚类。

模型是外置组件（85 MB，放 `<exe 同级>/extra/models/`），只有「深度」模式需要它。"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import __version__ as _VERSION
from . import group as G

from .opencv import EXTRA_DIRNAME, extra_dir, lazy_cv2 as cv2

MODEL_REPO = "Xenova/clip-vit-base-patch32"

MODEL_FILES = {
    "onnx/vision_model_quantized.onnx": "vision_model.onnx",
    "preprocessor_config.json": "preprocessor_config.json",
    "config.json": "config.json",
}

MODEL_FILES_FP32 = {
    "onnx/vision_model.onnx": "vision_model.onnx",
    "preprocessor_config.json": "preprocessor_config.json",
    "config.json": "config.json",
}

MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")



def model_root() -> Path:
    env = os.environ.get("IMGDEDUP_MODEL_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "imgdedup" / "models"


BUNDLE_SUBDIR = "models"


def repo_slug(repo: str = MODEL_REPO) -> str:
    return repo.replace("/", "__")


def model_extra_dir() -> Path:
    return extra_dir() / BUNDLE_SUBDIR


def bundled_root() -> Optional[Path]:
    cands: list[Path] = []
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:
        exe_dir = None
    if exe_dir is not None:
        cands.append(exe_dir / EXTRA_DIRNAME / BUNDLE_SUBDIR)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / BUNDLE_SUBDIR)
    if exe_dir is not None:
        cands.append(exe_dir / BUNDLE_SUBDIR)
        cands.append(exe_dir / "_internal" / BUNDLE_SUBDIR)
    here = Path(__file__).resolve().parent
    cands.append(here.parent / "resources" / BUNDLE_SUBDIR)
    cands.append(here / BUNDLE_SUBDIR)
    for c in cands:
        if c.is_dir():
            return c
    return None


def is_bundled(repo: str = MODEL_REPO, files: Optional[dict] = None) -> bool:
    files = files or MODEL_FILES
    b = bundled_root()
    return bool(b) and all((b / repo_slug(repo) / n).exists()
                           for n in files.values())


def model_dir(repo: str = MODEL_REPO, files: Optional[dict] = None) -> Path:
    files = files or MODEL_FILES
    slug = repo_slug(repo)
    b = bundled_root()
    if b is not None and all((b / slug / n).exists() for n in files.values()):
        return b / slug
    return model_root() / slug


def _download_file(url: str, dest: Path, *, quiet: bool = False,
                   cb: Optional[Callable] = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(
        url, headers={"User-Agent": f"imgdedup/{_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        t0 = time.perf_counter()
        last_cb = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if cb is not None and got - last_cb >= (2 << 20):
                last_cb = got
                cb(got, total, dest.name)
            if not quiet and total and got % (8 << 20) < (1 << 20):
                pct = 100.0 * got / total
                speed = got / max(1e-6, time.perf_counter() - t0) / 1024**2
                print(f"      {dest.name}: {pct:5.1f}%  {speed:5.1f} MB/s", flush=True)
        if cb is not None:
            cb(got, total or got, dest.name)
    tmp.replace(dest)


def is_ready(repo: str = MODEL_REPO, files: Optional[dict] = None) -> bool:
    files = files or MODEL_FILES
    d = model_dir(repo, files)
    return all((d / name).exists() for name in files.values())


def ensure_model(*, quiet: bool = False, repo: str = MODEL_REPO,
                 files: Optional[dict] = None,
                 dest: Optional[Path] = None,
                 cb: Optional[Callable] = None) -> Path:
    files = files or MODEL_FILES
    d = (dest / repo_slug(repo)) if dest is not None else model_dir(repo, files)
    missing = [(remote, local) for remote, local in files.items()
               if not (d / local).exists()]
    if not missing:
        return d

    if not quiet:
        where = "随程序目录" if is_bundled(repo, files) else "本地缓存"
        print(f"  {where}的模型不完整，缺失 {len(missing)} 个文件，"
              f"回退到联网下载（{repo}）")
    total_n = len(missing)
    last_err: Optional[Exception] = None
    for idx, (remote, local) in enumerate(missing):
        for mirror in MIRRORS:
            url = f"{mirror}/{repo}/resolve/main/{remote}"
            try:
                if not quiet:
                    print(f"    <- {mirror}  {remote}")
                _download_file(
                    url, d / local, quiet=quiet,
                    cb=(None if cb is None else
                        (lambda got, tot, name, _i=idx:
                         cb(got, tot, f"{name}（{_i + 1}/{total_n}）"))))
                break
            except Exception as e:
                last_err = e
                if not quiet:
                    print(f"       失败: {type(e).__name__} {str(e)[:60]}")
        else:
            raise RuntimeError(
                f"无法下载 {remote}（所有镜像均失败）: {last_err}")
    return d



@dataclass
class EmbedResult:
    vectors: Optional[np.ndarray] = None
    dim: int = 0
    backend: str = ""
    ok: bool = False
    note: str = ""


class ClipEncoder:

    def __init__(self, *, repo: str = MODEL_REPO, intra_threads: int = 0,
                 verbose: bool = False):
        self.repo = repo
        self.verbose = verbose
        self._sess = None
        self._input = None
        self._output = None
        self._mean = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
        self._std = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
        self._size = 224
        self._intra = intra_threads or max(1, (os.cpu_count() or 4) // 2)

    def load(self) -> bool:
        if self._sess is not None:
            return True
        try:
            import onnxruntime as ort
        except ImportError:
            if self.verbose:
                print("  onnxruntime 未安装，语义层不可用")
            return False
        d = model_dir(repo=self.repo)
        if not is_ready(repo=self.repo):
            if self.verbose:
                print(f"  模型未安装（外置组件）—— 应放到 {model_extra_dir()}")
                print("  取到本地： python -m dev.fetch_model")
            return False

        cfg_path = d / "preprocessor_config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                self._mean = np.array(cfg.get("image_mean", self._mean), np.float32)
                self._std = np.array(cfg.get("image_std", self._std), np.float32)
                sz = cfg.get("crop_size") or cfg.get("size") or {}
                if isinstance(sz, dict):
                    sz = sz.get("height") or sz.get("shortest_edge") or 224
                self._size = int(sz) if sz else 224
            except Exception:
                pass

        so = ort.SessionOptions()
        so.intra_op_num_threads = self._intra
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        self._sess = ort.InferenceSession(
            str(d / "vision_model.onnx"), sess_options=so,
            providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        outs = self._sess.get_outputs()
        names = [o.name for o in outs]
        self._output = next(
            (n for n in ("image_embeds", "pooler_output") if n in names),
            names[0])
        if self.verbose:
            print(f"  CLIP 已加载: input={self._input} output={self._output} "
                  f"({[o.name for o in outs]}) intra_op={self._intra}")
        return True

    @property
    def dim(self) -> int:
        if not self._sess:
            return 0
        return int(self._sess.get_outputs()[
            [o.name for o in self._sess.get_outputs()].index(self._output)
        ].shape[-1])

    def preprocess(self, rgb: np.ndarray) -> np.ndarray:
        s = self._size
        h, w = rgb.shape[:2]
        scale = s / min(h, w)
        nh, nw = max(s, int(round(h * scale))), max(s, int(round(w * scale)))
        img = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_CUBIC)
        top, left = (nh - s) // 2, (nw - s) // 2
        img = img[top:top + s, left:left + s]
        x = img.astype(np.float32) / 255.0
        x = (x - self._mean) / self._std
        return np.ascontiguousarray(x.transpose(2, 0, 1))

    def encode(self, thumbs: list[np.ndarray], *, batch: int = 16) -> EmbedResult:
        if not self.load():
            return EmbedResult(backend="none", note="onnxruntime 或模型不可用")
        vecs = []
        t0 = time.perf_counter()
        for i in range(0, len(thumbs), batch):
            chunk = thumbs[i:i + batch]
            arr = np.stack([self.preprocess(t) for t in chunk]).astype(np.float32)
            out = self._sess.run([self._output], {self._input: arr})[0]
            if out.ndim == 3:
                out = out.mean(axis=1)
            out = out.reshape(len(chunk), -1)
            vecs.append(out)
        v = np.concatenate(vecs).astype(np.float32)
        n = np.linalg.norm(v, axis=1, keepdims=True)
        v = v / np.maximum(n, 1e-8)
        return EmbedResult(vectors=v, dim=v.shape[1], backend="clip-onnx", ok=True,
                           note=f"{v.shape[0]} 张，{(time.perf_counter()-t0):.1f}s")


def encode_all(encoder: "ClipEncoder", thumbs: list[np.ndarray], *, batch: int = 16,
               on_progress: Optional[Callable[[int], None]] = None) -> EmbedResult:
    if not encoder.load():
        return EmbedResult(backend="none", note="onnxruntime 或模型不可用")
    chunks = []
    t0 = time.perf_counter()
    for i in range(0, len(thumbs), batch):
        part = thumbs[i:i + batch]
        r = encoder.encode(part, batch=batch)
        if not r.ok:
            return r
        chunks.append(r.vectors)
        if on_progress:
            on_progress(len(part))
    v = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros((0, 0), np.float32)
    return EmbedResult(
        vectors=v,
        dim=int(v.shape[1]) if v.size else 0,
        backend="clip-onnx",
        ok=True,
        note=f"{v.shape[0]} 张 / {time.perf_counter() - t0:.1f}s",
    )


def concat_results(parts: list["EmbedResult"]) -> EmbedResult:
    keep = [p for p in parts if p.ok and p.vectors is not None and p.vectors.size]
    if not keep:
        return EmbedResult(backend="clip-onnx", ok=True,
                           vectors=np.zeros((0, 0), np.float32), note="无可用嵌入")
    v = np.concatenate([p.vectors for p in keep]).astype(np.float32)
    return EmbedResult(vectors=v, dim=int(v.shape[1]), backend="clip-onnx",
                       ok=True, note=f"{v.shape[0]} 张")



DEFAULT_THRESHOLD = 0.97

DEFAULT_MAX_GROUP = 200


def cosine_groups(entries, vectors: np.ndarray, *, threshold: float = DEFAULT_THRESHOLD,
                  max_group: int = DEFAULT_MAX_GROUP, block: int = 512,
                  exclude: set[int] | None = None):
    idx = [i for i, e in enumerate(entries) if getattr(e, "embed_ok", False)]
    v = np.asarray(vectors, dtype=np.float32)
    if exclude:
        keep = [k for k, i in enumerate(idx) if i not in exclude]
        idx = [idx[k] for k in keep]
        v = v[keep]
    if len(idx) < 2:
        return [], []
    uf = G.UnionFind(len(idx))
    n = len(idx)
    for i0 in range(0, n, block):
        i1 = min(n, i0 + block)
        sim = v[i0:i1] @ v.T
        np.fill_diagonal(sim[:, i0:i1], -1.0)
        rows, cols = np.nonzero(sim >= threshold)
        for r, c in zip(rows.tolist(), cols.tolist()):
            a, b = i0 + r, c
            if b <= a:
                continue
            uf.union(a, b, float(sim[r, c]))

    groups, oversized = [], []
    for g in uf.groups(min_size=2):
        members = [idx[k] for k in g]
        if max_group and len(members) > max_group:
            oversized.append(members)
        else:
            groups.append(members)
    return groups, oversized
