"""扫描后台线程。

三档模式（fast / balanced / deep）的全部参数只在 `MODES` 一张表里定义。
界面文案、实际行为、`dev/run_cli.py` 都从它取，别在别处再写一遍。"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from PySide6.QtCore import QThread, Signal

from .model import GroupRow, build_merged_rows, build_rows, merged_label


MODES: dict[str, dict] = {
    "fast": {
        "name": "快速",
        "tag": "最快 · 只找完全相同的副本",
        "brief": "只比对「文件大小 + 头尾 128KB 指纹」，不读全文件、不解码图片。",
        "detects": "完全相同的副本（字节级一致）",
        "misses": "重压缩、改过尺寸、加过水印的近重复图",
        "cost": "秒级；CPU 只用来读几千字节",
        "index": 0,
        "kinds": ("exact",),
    },
    "balanced": {
        "name": "精确",
        "tag": "默认 · 完全副本 + 视觉近重复",
        "brief": "在「快速」基础上补全量哈希与感知特征（pHash / dHash / 直方图）。",
        "detects": "完全副本 + 缩放 / 轻微调色 / 重编码后仍相似的图",
        "misses": "构图相似但画面内容不同的图",
        "cost": "取决于图量；每张图解码一次到 256px",
        "index": 1,
        "kinds": ("exact", "perceptual"),
    },
    "deep": {
        "name": "深度",
        "tag": "最全 · 再加语义相似",
        "brief": "在「精确」基础上加 CLIP 语义向量，按余弦相似度聚类。",
        "detects": "上述全部 + 同场景 / 同主体但画面不同的图",
        "misses": "语义模型本身理解不了的抽象相似",
        "cost": "最高；每张图额外跑一次神经网络编码",
        "index": 2,
        "kinds": ("exact", "perceptual", "semantic", "cluster"),
    },
}

MODE_ORDER = ("fast", "balanced", "deep")


def mode_at(index: int) -> str:
    if 0 <= index < len(MODE_ORDER):
        return MODE_ORDER[index]
    return "balanced"


def mode_info(key: str) -> dict:
    return MODES.get(key, MODES["balanced"])


class ScanWorker(QThread):
    stage = Signal(str, int, int)
    log = Signal(str)
    finished_ok = Signal(object, object)
    failed = Signal(str)

    def __init__(self, root: str, *, semantic: bool = False, threshold: float = 0.97,
                 workers: int = 0, cache_dir: str = "", mode: str = "balanced",
                 parent=None):
        super().__init__(parent)
        self.root = root
        self.semantic = semantic
        self.threshold = threshold
        self._workers = workers
        self.cache_dir = cache_dir
        self.mode = mode
        if semantic and self.mode != "deep":
            self.mode = "deep"
        self.semantic = self.mode == "deep"
        self._cancel = False
        self._errors = 0
        self._pause = threading.Event()
        self._pause.set()

    def toggle_pause(self) -> bool:
        if self._pause.is_set():
            self._pause.clear()
            return True
        self._pause.set()
        return False

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    def cancel(self) -> None:
        self._cancel = True
        self._pause.set()

    def _gated(self, gate, weight, fn, item):
        self._pause.wait()
        if self._cancel:
            return None
        with gate.reserve(weight):
            return fn(item)

    def _map(self, items, fn, weight_fn, workers, gate, label) -> None:
        if not items:
            return
        total = len(items)
        every = max(1, total // 40)
        self.stage.emit(label, 0, total)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(self._gated, gate, weight_fn(it), fn, it) for it in items]
            for fut in as_completed(futs):
                if self._cancel:
                    return
                try:
                    fut.result()
                except Exception as e:
                    self._errors += 1
                    if self._errors <= 3:
                        import traceback
                        self.log.emit(f"[{label}] {type(e).__name__}: {e}")
                        self.log.emit(traceback.format_exc()[-500:])
                done += 1
                if done % every == 0 or done == total:
                    self.stage.emit(label, done, total)

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:
            import traceback

            self.failed.emit(f"{type(e).__name__}: {e}")
            self.log.emit(traceback.format_exc()[:1500])

    def _run(self) -> None:
        from imgdedup_core import decode as D
        from imgdedup_core import feature as F
        from imgdedup_core import group as G
        from imgdedup_core import scan as S
        from imgdedup_core.infra import MemGate, cpu_budget, default_memory_budget

        workers = self._workers or cpu_budget(0.75)
        gate = MemGate(default_memory_budget())
        meta: dict = {"root": self.root, "semantic": self.semantic,
                      "mode": self.mode,
                      "mode_name": MODES[self.mode]["name"]}

        # ── 阶段 1/7：遍历。只读目录项，不解码 ──────────────────────
        entries = []
        for e in S.walk(self.root):
            if self._cancel:
                return
            entries.append(e)
            if len(entries) % 500 == 0:
                self.stage.emit("遍历", len(entries), 0)
        self.stage.emit("遍历", len(entries), len(entries))
        meta["files"] = len(entries)
        meta["bytes"] = sum(e.size for e in entries)
        if not entries:
            self.finished_ok.emit([], meta)
            return

        from imgdedup_core import cache as K

        store = thumbs = None
        if self.cache_dir:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                store = K.FingerprintStore(os.path.join(self.cache_dir, "cache.db"))
                store.load()
                meta["cache_hits"] = store.apply(entries)
                thumbs = K.ThumbStore(self.cache_dir)
            except Exception as e:
                self.log.emit(f"缓存不可用，退化为全量计算：{type(e).__name__}")
                store = thumbs = None
        meta["cache_enabled"] = store is not None

        # ── 阶段 2/7：元数据。解析尺寸 / 格式，按 MemGate 限流并发 ──
        def do_meta(e):
            if e.width > 0:
                return e
            if not e.ext:
                e.ext = D.sniff_format(e.path)
            m = D.probe_meta(e.path, e.ext)
            if m:
                e.width, e.height, e.bands = m.width, m.height, m.bands
                e.fmt, e.meta_backend, e.meta_note = m.fmt, m.backend, m.note
            return e

        self._map(entries, do_meta, lambda e: 64 * 1024, workers, gate, "元数据")
        if self._cancel:
            return
        meta["resolved"] = sum(1 for e in entries if e.width > 0)

        # 大小唯一的文件不可能是重复项，先按大小分桶把候选缩掉
        buckets = S.bucket_by_size(entries)
        cand = [e for _s, v in buckets.items() if len(v) > 1 for e in v]
        meta["candidates"] = len(cand)

        # ── 阶段 3/7：短指纹。头尾各 64 KB，快速档到这一步就够 ──────
        def do_quick(e):
            if e.quick is not None:
                return e
            e.quick = F.quick_hash(e.path)
            return e

        self._map(cand, do_quick, lambda e: 512 * 1024, workers, gate, "短指纹")
        if self._cancel:
            return

        # 快速档到此为止：不再读全文件、不做感知特征，直接按「大小 + 短指纹」成组
        if self.mode == "fast":
            qb_fast: dict = {}
            for e in cand:
                if e.quick is not None:
                    qb_fast.setdefault((e.size, e.quick), []).append(e)
            idx_of = {id(e): i for i, e in enumerate(entries)}
            fast_groups = [[idx_of[id(x)] for x in v]
                           for v in qb_fast.values() if len(v) > 1]
            self.stage.emit("聚类", 1, 1)
            rows_fast: list[GroupRow] = build_rows(entries, fast_groups, "exact",
                                                   label_of=lambda *_: "字节级相同")
            meta["exact_groups"] = len(fast_groups)
            meta["perceptual_groups"] = 0
            meta["features_skipped_large"] = 0
            meta["worker_errors"] = self._errors
            meta["full_hash_skipped"] = True
            meta["perceptual_skipped"] = True
            if store is not None:
                try:
                    meta["cache_saved"] = store.save(entries)
                    store.close()
                except Exception as e:
                    self.log.emit(f"缓存写入失败：{type(e).__name__}")
            self.finished_ok.emit(rows_fast, meta)
            return

        qb: dict = {}
        for e in cand:
            if e.quick is not None:
                qb.setdefault((e.size, e.quick), []).append(e)
        narrowed = [e for v in qb.values() if len(v) > 1 for e in v]

        # ── 阶段 4/7：全量哈希。只有短指纹撞上的才需要读全文件 ──────
        def do_full(e):
            if e.full is not None:
                return e
            e.full = F.full_hash(e.path)
            return e

        self._map(narrowed, do_full, lambda e: 1024 * 1024, workers, gate, "全量哈希")
        if self._cancel:
            return

        # 硬链接是同一份数据的不同名字，删一个不影响另一个，
        # 所以要从精确组里排除，否则会把「没省下空间」的删除算成收益。
        hl = S.find_hardlink_groups(entries)
        hl_idx: set[int] = set()
        if hl:
            by_path = {e.path: i for i, e in enumerate(entries)}
            for paths in hl.values():
                for p in paths:
                    hl_idx.add(by_path[p])
        meta["hardlink_paths"] = len(hl_idx)
        exact_groups = G.exact_groups(entries, hardlink_protected=hl_idx)

        # ── 阶段 5/7：感知特征。解码到 256px 后取 pHash / dHash / 直方图 ──
        def do_feat(e):
            if e.feat_cached:
                return e
            if e.width <= 0:
                return e
            if K.should_skip_features(e.pixels):
                e.feat_ok = False
                e.feat_note = "超大图，已跳过感知哈希（仍参与精确哈希）"
                return e
            if thumbs is not None:
                arr, backend = thumbs.thumb_for(e, 256)
            else:
                arr, backend = D.load_small(e.path, 256, e.ext, e.pixels)
            f = F.extract(arr, backend, e.meta_note)
            e.phash, e.dhash, e.hist = f.phash, f.dhash, f.hist
            e.feat_ok, e.feat_note, e.thumb_backend = f.ok, f.note, backend
            return e

        self._map(entries, do_feat,
                  lambda e: D.estimate_decode_bytes(D.Meta(e.width, e.height), 256),
                  workers, gate, "感知特征")
        if self._cancel:
            return

        # ── 阶段 6/7：语义。CLIP 向量 + 余弦聚类，只有深度档会跑 ────
        self.stage.emit("聚类", 0, 0)
        sem_groups: list[list[int]] = []
        sem_oversized: list[list[int]] = []
        if self.semantic:
            sem_groups, sem_oversized = self._semantic(entries, gate, workers)
        quarantined = {i for g in sem_oversized for i in g}

        exact_groups = [g for g in exact_groups if not (set(g) & quarantined)]
        perc_groups = G.perceptual_groups(entries, exclude=quarantined)

        # ── 阶段 7/7：聚类。三层依次加边，分量取内部最弱关系 ────────
        lc = G.LayeredClusters(len(entries))
        lc.add_layer(exact_groups, "exact")
        lc.add_layer(perc_groups, "perceptual")
        lc.add_layer(sem_groups, "semantic")
        components = lc.components()

        def row_label(kind: str, n_units: int, n_free: int) -> str:
            label = merged_label(kind, n_units, n_free)
            if label == kind == "semantic":
                return f"余弦 ≥ {self.threshold:.2f}"
            return label

        rows: list[GroupRow] = build_merged_rows(entries, components,
                                                 exact_groups,
                                                 label_of=row_label)
        rows += build_rows(entries, sem_oversized, "cluster",
                           label_of=lambda *_: "过合并，需人工确认",
                           oversized=True)

        self.stage.emit("聚类", 1, 1)
        by_kind: dict[str, int] = {}
        for k, _members in components:
            by_kind[k] = by_kind.get(k, 0) + 1
        meta["exact_groups"] = by_kind.get("exact", 0)
        meta["perceptual_groups"] = by_kind.get("perceptual", 0)
        meta["semantic_groups"] = by_kind.get("semantic", 0)
        meta["quarantined_groups"] = len(sem_oversized)
        meta["layer_merges"] = {k: lc.merges(k)
                                for k in ("exact", "perceptual", "semantic")}
        meta["features_skipped_large"] = sum(
            1 for e in entries if "超大图" in (e.feat_note or ""))
        meta["worker_errors"] = self._errors

        if store is not None:
            try:
                meta["cache_saved"] = store.save(entries)
                meta["cache_stats"] = store.stats()
                store.close()
            except Exception as e:
                self.log.emit(f"缓存写入失败：{type(e).__name__}")
        if thumbs is not None:
            meta["thumb_stats"] = thumbs.stats()

        self.finished_ok.emit(rows, meta)

    def _semantic(self, entries, gate, workers
                  ) -> tuple[list[list[int]], list[list[int]]]:
        from imgdedup_core import decode as D
        from imgdedup_core import embed as EMB

        enc = EMB.ClipEncoder()
        if not enc.load():
            self.log.emit("CLIP 不可用（缺 onnxruntime 或模型），跳过语义层")
            return [], []

        holder: dict = {}
        CHUNK = 1024
        parts = []
        total = len(entries)
        done = 0
        self.stage.emit("语义", 0, total)

        for c0 in range(0, total, CHUNK):
            if self._cancel:
                return [], []
            chunk = entries[c0:c0 + CHUNK]
            holder.clear()

            def do_thumb(e, _h=holder):
                if e.width <= 0:
                    return e
                arr, _b = D.load_small(e.path, 224, e.ext, e.pixels)
                if arr is not None:
                    _h[id(e)] = arr
                return e

            self._map(chunk, do_thumb,
                      lambda e: D.estimate_decode_bytes(D.Meta(e.width, e.height), 224),
                      workers, gate, "语义")
            usable = [e for e in chunk if id(e) in holder]
            if not usable:
                continue
            res = EMB.encode_all(enc, [holder[id(e)] for e in usable], batch=16)
            if not res.ok:
                self.log.emit(f"语义编码失败：{res.note}")
                return [], []
            for e in usable:
                e.embed_ok = True
            parts.append(res)
            done += len(usable)
            self.stage.emit("语义", done, total)

        combined = EMB.concat_results(parts)
        return EMB.cosine_groups(entries, combined.vectors,
                                 threshold=self.threshold)
