"""语义相似度层（M6）：CLIP ONNX 图像嵌入 + 余弦聚类。

为什么用 CLIP：感知哈希（pHash/dHash）只能发现"同一张图的不同版本"，
对"同一场景的不同拍摄""同一素材的不同裁切/水印"无能为力。
CLIP 图像嵌入把画面映射到语义空间，靠余弦相似度能找到这类关系。

本机环境约束（实测）：
  * `huggingface.co` 超时，`hf-mirror.com` 可达（0.4 s）→ 默认走镜像；
  * 本机原本没有任何推理运行时，`onnxruntime` 是 M6 唯一新增依赖；
  * 模型按需下载，不进发布产物，缓存在用户目录而非项目目录
    （避免把 85 MB 模型塞进项目、也避免被 .scratch 清理误伤）。

模型选型：`Xenova/clip-vit-base-patch32` 的 int8 量化 vision encoder，
85 MB（fp32 是 335 MB）。量化对"排序相似度"的影响在 1–2% 量级，
对这个用途足够；真要更高精度可切 `MODEL_FILES_FP32`。
"""

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

# 版本号只从包根取，不在别处再写一份字面量（`imgdedup_core/__init__.py`
# 是唯一来源）。这个串会出现在模型下载的 User-Agent 里。
from . import __version__ as _VERSION
from . import group as G

# 不要写 `import cv2`。cv2 已改为外置可选组件（86 MB，不随包分发），
# 顶层硬导入会在缺组件时让整个程序连窗口都建不出来：用户只看到闪退、
# 没有任何提示。用惰性代理：第一次用到 `cv2.xxx` 时才去定位并导入，
# 缺了会抛出带操作说明的 ImportError（见 `opencv.missing_hint`）。
from .opencv import EXTRA_DIRNAME, extra_dir, lazy_cv2 as cv2

MODEL_REPO = "Xenova/clip-vit-base-patch32"

#: 量化版（默认）
MODEL_FILES = {
    "onnx/vision_model_quantized.onnx": "vision_model.onnx",
    "preprocessor_config.json": "preprocessor_config.json",
    "config.json": "config.json",
}

#: 全精度版（可选，改这个再删缓存即可切换）
MODEL_FILES_FP32 = {
    "onnx/vision_model.onnx": "vision_model.onnx",
    "preprocessor_config.json": "preprocessor_config.json",
    "config.json": "config.json",
}

MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")


# ================================================================ 模型缓存

def model_root() -> Path:
    """模型缓存目录。可用 IMGDEDUP_MODEL_DIR 覆盖。

    刻意放在用户目录而不是项目目录：模型是跨项目共享的资源，
    放项目里既会让项目体积膨胀，也容易被"清理项目垃圾"的操作误伤。
    """
    env = os.environ.get("IMGDEDUP_MODEL_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "imgdedup" / "models"


#: 模型子目录名（相对 `extra/` / 解包根 / exe 同级 / 项目根）
BUNDLE_SUBDIR = "models"


def repo_slug(repo: str = MODEL_REPO) -> str:
    return repo.replace("/", "__")


def model_extra_dir() -> Path:
    """推荐给用户的模型摆放位置：`<exe 同级>/extra/models`。

    与 cv2 共用同一个 `extra/` 目录（cv2 在 `<exe 同级>/extra/cv2`）——
    外置组件只有一套约定，见 `imgdedup_core.opencv.extra_dir()`。
    """
    return extra_dir() / BUNDLE_SUBDIR


def bundled_root() -> Optional[Path]:
    """随程序分发的模型根目录（发布形态下优先于下载缓存）。

    名字保留 `bundled_` 是历史原因。模型现在是外置组件
    （和 cv2 一样不打进安装包），所以第一位候选就是 `extra/models`。

    按候选顺序探测，覆盖五种落地形态：
      1. exe 同级的 `extra/models`：推荐位置（免安装版 / 安装版）
      2. PyInstaller 解包目录（sys._MEIPASS）
      3. 可执行文件同级（onefile）
      4. exe 的 `_internal` 子目录（onedir）
      5. 源码树 `resources/models`（开发态）
    """
    cands: list[Path] = []
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:  # noqa: BLE001
        exe_dir = None
    if exe_dir is not None:
        # 第一位：用户"自己放好的"必须优先于其它可能存在的副本，
        # 否则会出现"明明放了却没用上"。
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
    """当前是否会用随程序分发的模型（`extra/models` 等位置）。

    模型是外置组件，正常安装包里不会有它，所以发布形态下
    返回 False 是正常状态，不是错误。仅用于报告与测试。
    """
    files = files or MODEL_FILES
    b = bundled_root()
    return bool(b) and all((b / repo_slug(repo) / n).exists()
                           for n in files.values())


def model_dir(repo: str = MODEL_REPO, files: Optional[dict] = None) -> Path:
    """模型目录。随程序目录优先，其次下载缓存。

    只有整套文件齐全时才采用随程序目录。部分缺失时退回缓存目录 ——
    这样 `ensure_model` 的补下会写进用户可写的缓存，而不是往解包目录里写
    （onefile 模式下 `_MEIPASS` 是临时且只读的）。

    想"下到 exe 旁边"要显式给 `ensure_model(dest=model_extra_dir())`，
    别指望这里返回 extra 目录：它只保证"哪里能读到"。
    """
    files = files or MODEL_FILES
    slug = repo_slug(repo)
    b = bundled_root()
    if b is not None and all((b / slug / n).exists() for n in files.values()):
        return b / slug
    return model_root() / slug


def _download_file(url: str, dest: Path, *, quiet: bool = False,
                   cb: Optional[Callable] = None) -> None:
    """下载单个文件。

    `cb(got, total, name)` 用来把进度回给 UI（可为 None）。
    回调必须节流：85 MB 的模型若每读一块就回一次，
    会把主线程的事件队列刷爆（跨线程投递是有成本的）。
    """
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
    """取得模型目录，按优先级解析：

      1. `extra/models`（外置组件，推荐位置，不联网）
      2. 其它随程序分发的候选位置（见 `bundled_root()`）
      3. 本地缓存 `~/.cache/imgdedup/models`
      4. 都缺 → 联网从镜像下载

    `dest` 指定模型根目录（默认走 `model_dir()` 的解析结果）。
    传 `model_extra_dir()` 就是"下到 exe 旁边的 `extra/models`" ——
    这样落地位置与提示文案里的路径一致，不会出现"下完了却找不到"。

    `dest` 的语义是根目录（如 `extra/models`），内部会再拼一层
    `repo_slug`，与 `model_dir()` 的解析结果对齐。少拼这一层的话，
    文件会落到 `extra/models/vision_model.onnx`，而读取路径找的是
    `extra/models/<slug>/vision_model.onnx`：下载成功却永远读不到。

    `cb(got, total, msg)` 把进度回给 UI，与 cv2 的下载回调同一形状
    （`got/total` 是当前文件的字节数，`msg` 形如 `xxx.onnx（1/3）`），
    所以两边的进度对话框可以共用一套代码。

    返回装着模型文件的那个目录（= `dest / repo_slug`），
    与 `model_dir()` 的返回值同义。
    """
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
            except Exception as e:  # noqa: BLE001
                last_err = e
                if not quiet:
                    print(f"       失败: {type(e).__name__} {str(e)[:60]}")
        else:
            raise RuntimeError(
                f"无法下载 {remote}（所有镜像均失败）: {last_err}")
    return d


# ================================================================ 编码器

@dataclass
class EmbedResult:
    vectors: Optional[np.ndarray] = None   # (N, D) float32，已 L2 归一化
    dim: int = 0
    backend: str = ""
    ok: bool = False
    note: str = ""


class ClipEncoder:
    """CLIP 图像编码器（ONNX Runtime，CPU）。

    线程策略：ONNX 自身用多线程（intra_op），上层按 batch 串行喂数据。
    比"多个 Python 线程各跑 batch=1"更划算——ViT 的算力密度高，
    攒 batch 才能吃满 SIMD。
    """

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

    # ---------------------------------------------------------- 生命周期
    def load(self) -> bool:
        if self._sess is not None:
            return True
        try:
            import onnxruntime as ort
        except ImportError:
            if self.verbose:
                print("  onnxruntime 未安装，语义层不可用")
            return False
        # 这里刻意不联网。
        #
        # 模型是外置组件，下载由 UI 显式发起
        # （`_entry_symbols.ensure_model_ready` → 带进度对话框）。
        # 让后台的扫描线程悄悄下 85 MB 是灾难级体验：用户看不到任何进度，
        # 只会觉得"卡住了"；而测试 / CI 上更会变成"跑一遍测试顺便下模型"——
        # 实测模拟全新克隆（没有模型）时，整个测试套件卡在网络上十几分钟。
        #
        # 想联网取模型请显式调 `ensure_model()`，或跑 `python -m dev.fetch_model`。
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
            except Exception:  # noqa: BLE001
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
        # 优先取已经投影到 512 维的 image_embeds；否则退到 pooler / hidden state
        names = [o.name for o in outs]
        self._output = next(
            (n for n in ("image_embeds", "pooler_output") if n in names),
            names[0])
        if self.verbose:
            print(f"  CLIP 已加载: input={self._input} output={self._output} "
                  f"({[o.name for o in outs]}) intra_op={self._intra}")
        return True

    @property
    def ready(self) -> bool:
        return self._sess is not None

    @property
    def dim(self) -> int:
        if not self._sess:
            return 0
        return int(self._sess.get_outputs()[
            [o.name for o in self._sess.get_outputs()].index(self._output)
        ].shape[-1])

    # ---------------------------------------------------------- 预处理
    def preprocess(self, rgb: np.ndarray) -> np.ndarray:
        """按 CLIP 的标准流程：短边缩放 → 中心裁剪 → 归一化 → NCHW。"""
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

    # ---------------------------------------------------------- 推理
    def encode(self, thumbs: list[np.ndarray], *, batch: int = 16) -> EmbedResult:
        """thumbs: 一组 RGB uint8 数组。返回 L2 归一化后的嵌入。"""
        if not self.load():
            return EmbedResult(backend="none", note="onnxruntime 或模型不可用")
        vecs = []
        t0 = time.perf_counter()
        for i in range(0, len(thumbs), batch):
            chunk = thumbs[i:i + batch]
            arr = np.stack([self.preprocess(t) for t in chunk]).astype(np.float32)
            out = self._sess.run([self._output], {self._input: arr})[0]
            if out.ndim == 3:            # last_hidden_state -> 平均池化
                out = out.mean(axis=1)
            out = out.reshape(len(chunk), -1)
            vecs.append(out)
        v = np.concatenate(vecs).astype(np.float32)
        # L2 归一化后，点积即余弦相似度
        n = np.linalg.norm(v, axis=1, keepdims=True)
        v = v / np.maximum(n, 1e-8)
        return EmbedResult(vectors=v, dim=v.shape[1], backend="clip-onnx", ok=True,
                           note=f"{v.shape[0]} 张，{(time.perf_counter()-t0):.1f}s")


def encode_all(encoder: "ClipEncoder", thumbs: list[np.ndarray], *, batch: int = 16,
               on_progress: Optional[Callable[[int], None]] = None) -> EmbedResult:
    """批量编码整批缩略图。

    把 batching 收在这里，调用方不必引入 numpy，也不必知道
    ONNX 的线程模型（session 内部多线程 + 上层串行喂 batch）。
    """
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
    """把分块编码的结果拼成完整嵌入矩阵。

    存在的意义是让调用方能「分块解码 → 分块编码 → 立刻释放缩略图」，
    而不是把全库缩略图同时压在内存里：224x224x3 每张 147 KB，
    1 万张就是 1.4 GB，远超内存预算。
    嵌入本身只占 512 x 4 B = 2 KB/张，1 万张才 20 MB，可以全留。
    """
    keep = [p for p in parts if p.ok and p.vectors is not None and p.vectors.size]
    if not keep:
        return EmbedResult(backend="clip-onnx", ok=True,
                           vectors=np.zeros((0, 0), np.float32), note="无可用嵌入")
    v = np.concatenate([p.vectors for p in keep]).astype(np.float32)
    return EmbedResult(vectors=v, dim=int(v.shape[1]), backend="clip-onnx",
                       ok=True, note=f"{v.shape[0]} 张")


# ================================================================ 聚类

#: 默认余弦阈值。这个值必须按图库调，不能当成通用常数。
#:
#: 实测教训：在一个同题材图库（3,197 张）上，
#:   两两余弦 p50=0.773 / p99=0.916 / p99.9=0.946；
#:   中位图的最近邻相似度是 0.9867：一半的图都能找到 ≥0.9867 的近邻。
#: 用 0.92 做阈值会合并出 1554 张的巨型组（半个图库）。
#: 阈值扫描的拐点在 0.97：753 组 / 最大组 16 / 无 >20 的组。
#: 换成"背景 + 余量"的相对判据并不更省心：余量同样要调到 0.20 才安全。
DEFAULT_THRESHOLD = 0.97

#: 组规模护栏。超过这个规模的连通分量不是重复组，而是"聚类"，
#: 必须单独隔离、不参与批量删除。这是语义去重最危险的地方：
#: 过合并（over-merge）会一次性把半个图库标记成"可删除"。
DEFAULT_MAX_GROUP = 200


def cosine_groups(entries, vectors: np.ndarray, *, threshold: float = DEFAULT_THRESHOLD,
                  max_group: int = DEFAULT_MAX_GROUP, block: int = 512,
                  exclude: set[int] | None = None):
    """按余弦相似度聚类。返回 (groups, oversized)。

    groups    : 可当作"重复组"呈现给用户的连通分量
    oversized : 被组规模护栏拦下的超大连通分量，只能作为"聚类"展示，
                绝不允许批量删除
    exclude   : 这些下标不参与聚类。调用方用它排除已经在更强的分组里出现过的
                文件（理由见 `group.perceptual_groups` 的说明）。
                注意 `vectors` 的第 k 行对应 `idx` 的第 k 项，所以下标和向量
                必须同步过滤 —— 只滤一边会让分组指到别的文件身上。

    复杂度：N=1 万、D=512 时约 5x10^10 次乘加，矩阵乘 + 分块后
    单次全量扫描在 12 核上约 1–2 秒，不需要 HNSW。
    分块是为了避免一次分配 N^2 的矩阵（1 万 → 1 亿个 float）。
    """
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
        sim = v[i0:i1] @ v.T                      # (block, n) 余弦
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
