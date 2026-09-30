"""后台扫描线程。

两条铁律：
  1. 扫描绝不在主线程做：那会直接冻住界面；
  2. 核心引擎绝不在主线程导入：cv2/numpy 的导入是秒级的，
     放在启动路径上会毁掉「首屏 < 1s」的目标。所以所有 core 导入都写在
     `_run()` 里面（也就是工作线程里）。

进度按阶段上报，不做成一根总进度条：见 widgets.StageProgress 的说明。
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from PySide6.QtCore import QThread, Signal

from .model import GroupRow, build_rows


#: 三档扫描模式。这张表是唯一事实来源：UI 的说明文案、`on_start()` 的
#: 参数、本模块的流水线分支都从这里派生，避免"下拉框写得一套、实际跑另一套"。
#:
#: 这里修的是一个真 bug：早先只有 `semantic = index == 2` 一处判断，
#: 于是"快速"和"精确"跑的是完全相同的流水线（都做全量哈希）——
#: 下拉框里的"精确"是假的，用户选出"快速"并没有更快。
#:
#: 三档的真实差异（按耗时从低到高）：
#:   * `fast`     ：不读全文件、不解码像素，只按「尺寸 + 头尾 128KB 短指纹」
#:                    分组。秒级出结果，代价是只能发现完全相同的副本，
#:                    漏掉重压缩/改过尺寸的近重复图。也不做感知分组。
#:   * `balanced` ：在 fast 基础上补「全量哈希」（逐字节精确判定）+
#:                    「感知特征」（pHash/dHash/直方图，抓近重复）。默认档。
#:   * `deep`     ：在 balanced 基础上加「CLIP 语义向量余弦聚类」，
#:                    能抓到"构图相似但视觉不同"的图（如同一场景不同角度）。
#:                    需要 onnxruntime + 模型，缺了会自动降级并在日志里说明。
MODES: dict[str, dict] = {
    "fast": {
        "name": "快速",
        "tag": "最快 · 只找完全相同的副本",
        "brief": "只比对「文件大小 + 头尾 128KB 指纹」，不读全文件、不解码图片。",
        "detects": "完全相同的副本（字节级一致）",
        "misses": "重压缩、改过尺寸、加过水印的近重复图",
        "cost": "秒级；CPU 只用来读几千字节",
        "index": 0,
    },
    "balanced": {
        "name": "精确",
        "tag": "默认 · 完全副本 + 视觉近重复",
        "brief": "在「快速」基础上补全量哈希与感知特征（pHash / dHash / 直方图）。",
        "detects": "完全副本 + 缩放 / 轻微调色 / 重编码后仍相似的图",
        "misses": "构图相似但画面内容不同的图",
        "cost": "取决于图量；每张图解码一次到 256px",
        "index": 1,
    },
    "deep": {
        "name": "深度",
        "tag": "最全 · 再加语义相似",
        "brief": "在「精确」基础上加 CLIP 语义向量，按余弦相似度聚类。",
        "detects": "上述全部 + 同场景 / 同主体但画面不同的图",
        "misses": "语义模型本身理解不了的抽象相似",
        "cost": "最高；每张图额外跑一次神经网络编码",
        "index": 2,
    },
}

#: 下拉框顺序（index → mode key）。改顺序必须同时改 UI 的 `addItems` 顺序。
MODE_ORDER = ("fast", "balanced", "deep")


def mode_at(index: int) -> str:
    """下拉框 index → mode key。越界回落到默认档，别抛。"""
    if 0 <= index < len(MODE_ORDER):
        return MODE_ORDER[index]
    return "balanced"


def mode_info(key: str) -> dict:
    return MODES.get(key, MODES["balanced"])


class ScanWorker(QThread):
    stage = Signal(str, int, int)          # 阶段名, 已完成, 总数（总数 0 = 未定）
    log = Signal(str)
    finished_ok = Signal(object, object)   # rows, meta
    failed = Signal(str)

    STAGE_ORDER = ("遍历", "元数据", "短指纹", "全量哈希", "感知特征", "语义", "聚类")

    def __init__(self, root: str, *, semantic: bool = False, threshold: float = 0.97,
                 workers: int = 0, cache_dir: str = "", mode: str = "balanced",
                 parent=None):
        super().__init__(parent)
        self.root = root
        self.semantic = semantic
        self.threshold = threshold
        self._workers = workers
        self.cache_dir = cache_dir
        #: `fast` | `balanced` | `deep`：见 `MODES`。
        #: `semantic` 参数保留只为兼容旧调用点，真实语义层由 `mode` 决定，
        #: `semantic=True` 会强制抬成 `deep`（见下方归一化）。
        self.mode = mode
        if semantic and self.mode != "deep":
            self.mode = "deep"
        self.semantic = self.mode == "deep"
        self._cancel = False
        self._errors = 0
        # 暂停用 Event（置位 = 运行）。工作线程在取任务前等它，
        # 所以暂停时不占 CPU、也不会丢掉已完成的进度。
        self._pause = threading.Event()
        self._pause.set()

    # ---------------------------------------------------------- 控制
    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    def toggle_pause(self) -> bool:
        """切换暂停，返回切换后是否处于暂停状态。"""
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
        # 必须同时解除暂停，否则工作线程会一直卡在 _pause.wait() 里。
        self._pause.set()

    # ---------------------------------------------------------- 并发工具
    def _gated(self, gate, weight, fn, item):
        # 暂停：在这里等（不占 CPU、不丢进度）；取消：放弃剩余任务。
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
                except Exception as e:  # noqa: BLE001
                    # 绝不静默吞掉。实测踩过：感知阶段整段抛异常被吞，
                    # 结果是「耗时 2.77s、感知组 0」，看起来正常跑完了，
                    # 实际结论是错的。前几条必须打出来。
                    self._errors += 1
                    if self._errors <= 3:
                        import traceback
                        self.log.emit(f"[{label}] {type(e).__name__}: {e}")
                        self.log.emit(traceback.format_exc()[-500:])
                done += 1
                if done % every == 0 or done == total:
                    self.stage.emit(label, done, total)

    # ---------------------------------------------------------- 主流程
    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # noqa: BLE001
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

        # ---------------- 遍历 ----------------
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

        # ---------------- 打开两级缓存并回填 ----------------
        # 命中就把上一轮算好的元数据/哈希/感知特征直接填回来，
        # 各阶段用「工作函数开头早返回」跳过，这是增量重扫能进秒级的原因。
        from imgdedup_core import cache as K

        store = thumbs = None
        if self.cache_dir:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                store = K.FingerprintStore(os.path.join(self.cache_dir, "cache.db"))
                store.load()
                meta["cache_hits"] = store.apply(entries)
                thumbs = K.ThumbStore(self.cache_dir)
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"缓存不可用，退化为全量计算：{type(e).__name__}")
                store = thumbs = None
        meta["cache_enabled"] = store is not None

        # ---------------- 元数据 ----------------
        def do_meta(e):
            if e.width > 0:
                return e                      # 缓存命中，跳过
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

        # ---------------- 候选分桶（三档共用）----------------
        # 这一步必须在 `fast` 分支之前：`cand` 是"可能重复"的候选集，
        # 三档都要用。早先把它写在 `fast` 分支后面，于是 fast 档一进分支就
        # `UnboundLocalError: cand`，端到端一跑就炸（单元测试反而测不到，
        # 因为只断言了参数、没真扫）。
        buckets = S.bucket_by_size(entries)
        cand = [e for _s, v in buckets.items() if len(v) > 1 for e in v]
        meta["candidates"] = len(cand)

        def do_quick(e):
            if e.quick is not None:
                return e                      # 缓存命中，跳过
            e.quick = F.quick_hash(e.path)
            return e

        self._map(cand, do_quick, lambda e: 512 * 1024, workers, gate, "短指纹")
        if self._cancel:
            return

        # ---------------- 快速档：到此为止 ----------------
        # 「快速」只信「尺寸 + 头尾短指纹」，不读全文件、不解码像素。
        # 命中这一档就直接按 (size, quick) 分组返回，这是三档里唯一
        # 真正的"秒级"路径。代价见 `MODES["fast"]["misses"]`。
        if self.mode == "fast":
            qb_fast: dict = {}
            for e in cand:
                if e.quick is not None:
                    qb_fast.setdefault((e.size, e.quick), []).append(e)
            # 用 `id()` 建索引表而不是 `entries.index(x)`：后者是 O(n)，
            # 组一多就是 O(n²)。也别复用下面硬链接那段的 `by_path`
            # （那是 path→index，语义不同，复用会踩到同名覆盖）。
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
                except Exception as e:  # noqa: BLE001
                    self.log.emit(f"缓存写入失败：{type(e).__name__}")
            self.finished_ok.emit(rows_fast, meta)
            return

        # ---------------- 精确去重（短指纹 → 全量）----------------
        qb: dict = {}
        for e in cand:
            if e.quick is not None:
                qb.setdefault((e.size, e.quick), []).append(e)
        narrowed = [e for v in qb.values() if len(v) > 1 for e in v]

        def do_full(e):
            if e.full is not None:
                return e                      # 缓存命中，跳过
            e.full = F.full_hash(e.path)
            return e

        self._map(narrowed, do_full, lambda e: 1024 * 1024, workers, gate, "全量哈希")
        if self._cancel:
            return

        hl = S.find_hardlink_groups(entries)
        hl_idx: set[int] = set()
        if hl:
            by_path = {e.path: i for i, e in enumerate(entries)}
            for paths in hl.values():
                for p in paths:
                    hl_idx.add(by_path[p])
        meta["hardlink_paths"] = len(hl_idx)
        exact_groups = G.exact_groups(entries, hardlink_protected=hl_idx)

        # ---------------- 感知特征 ----------------
        def do_feat(e):
            if e.feat_cached:
                return e                      # 缓存命中，跳过
            if e.width <= 0:
                return e
            if K.should_skip_features(e.pixels):
                # 超大图的成本在熵解码，降目标尺寸没用（实测 233MP 的 JPEG
                # 缩略图 210 ms，目标 256 与 64 几乎无差）。唯一有效的降级是跳过。
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

        self.stage.emit("聚类", 0, 0)
        # 每个文件只进"最强"的那一类：精确 > 感知 > 语义。
        # 字节相同的图必然也满足感知判定（实测 pHash / dHash / 直方图距离全是 0），
        # 不排除就会让同一批文件同时出现在两类里 —— 用户按两类各删一遍，
        # 而"可释放空间"被算了两遍。
        grouped = {i for g in exact_groups for i in g}
        perc_groups = G.perceptual_groups(entries, exclude=grouped)
        grouped |= {i for g in perc_groups for i in g}

        rows: list[GroupRow] = []
        rows += build_rows(entries, exact_groups, "exact")
        rows += build_rows(entries, perc_groups, "perceptual")

        # ---------------- 语义（可选）----------------
        if self.semantic:
            rows += self._semantic(entries, gate, workers, exclude=grouped)

        self.stage.emit("聚类", 1, 1)
        meta["exact_groups"] = len(exact_groups)
        meta["perceptual_groups"] = len(perc_groups)
        meta["features_skipped_large"] = sum(
            1 for e in entries if "超大图" in (e.feat_note or ""))
        meta["worker_errors"] = self._errors

        # ---------------- 回写缓存 ----------------
        if store is not None:
            try:
                meta["cache_saved"] = store.save(entries)
                meta["cache_stats"] = store.stats()
                store.close()
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"缓存写入失败：{type(e).__name__}")
        if thumbs is not None:
            meta["thumb_stats"] = thumbs.stats()

        self.finished_ok.emit(rows, meta)

    def _semantic(self, entries, gate, workers, *,
                  exclude: set[int] | None = None) -> list[GroupRow]:
        from imgdedup_core import decode as D
        from imgdedup_core import embed as EMB

        enc = EMB.ClipEncoder()
        if not enc.load():
            self.log.emit("CLIP 不可用（缺 onnxruntime 或模型），跳过语义层")
            return []

        holder: dict = {}
        CHUNK = 1024
        parts = []
        total = len(entries)
        done = 0
        self.stage.emit("语义", 0, total)

        for c0 in range(0, total, CHUNK):
            if self._cancel:
                return []
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
                return []
            for e, v in zip(usable, res.vectors):
                e.embed = v
                e.embed_ok = True
            parts.append(res)
            done += len(usable)
            self.stage.emit("语义", done, total)

        combined = EMB.concat_results(parts)
        groups, oversized = EMB.cosine_groups(entries, combined.vectors,
                                              threshold=self.threshold,
                                              exclude=exclude)
        rows = build_rows(entries, groups, "semantic",
                          label_of=lambda *_: f"余弦 ≥ {self.threshold:.2f}")
        rows += build_rows(entries, oversized, "cluster",
                           label_of=lambda *_: "过合并，需人工确认",
                           oversized=True)
        return rows
