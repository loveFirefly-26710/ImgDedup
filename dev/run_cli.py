"""开发期命令行：跑一遍完整流程。

这不是产品。 最终交付只有 GUI（python -m imgdedup_ui）。
本脚本存在的意义是：能在真实数据上跑基准、能进 CI 做断言，
这是 GUI 点不出来的能力。

用法（在项目根目录）：
    python -m dev.run_cli --root E:\\1\\test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imgdedup_core import catalog as C  # noqa: E402
from imgdedup_core import decode as D  # noqa: E402
from imgdedup_core import feature as F  # noqa: E402
from imgdedup_core import group as G  # noqa: E402
from imgdedup_core import scan as S  # noqa: E402
from imgdedup_core import scratch as SC  # noqa: E402
from imgdedup_core.infra import (  # noqa: E402
    MemGate,
    Progress,
    Timings,
    cpu_budget,
    default_memory_budget,
    human_bytes,
    human_secs,
)

RULE = "=" * 78


def hr(title: str = "") -> None:
    if title:
        print(f"\n{RULE}\n  {title}\n{RULE}")
    else:
        print(RULE)


# ================================================================ 并发工具

def _gated(gate: MemGate, weight: int, fn, item):
    with gate.reserve(weight):
        return fn(item)


def map_concurrent(items, fn, weight_fn, workers: int, gate: MemGate, label: str):
    """带内存闸门的并发映射。结果按完成顺序返回。"""
    prog = Progress(label, len(items), every=max(1, len(items) // 8))
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_gated, gate, weight_fn(it), fn, it): it for it in items}
        for fut in as_completed(futs):
            out.append((futs[fut], fut.result()))
            prog.tick()
    return out


# ================================================================ 阶段 1：能力探测

def stage1_capability() -> dict:
    hr("阶段 1 · 解码后端能力探测")
    summ = D.BACKENDS.summary()
    for k, v in summ.items():
        print(f"  {k:<10} {v}")
    missing = [k for k in ("pyvips", "Pillow", "pypdfium2") if k not in summ]
    if missing:
        print(f"  ! 缺失后端: {missing}")
    return summ


# ================================================================ 阶段 2：格式矩阵实测

def _find_real(patterns: tuple[str, ...], roots: tuple[str, ...], limit: int = 1) -> list[str]:
    import glob

    hits: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for pat in patterns:
            hits += glob.glob(os.path.join(root, "**", pat), recursive=True)
            if len(hits) >= limit:
                return hits[:limit]
    return hits[:limit]


def stage2_format_matrix(sp_dir: str, root: str) -> list[dict]:
    """实测每种格式能否解码 + 取元数据。

    样本来源策略（在「无法提供 PSD/AI/BMP/TIFF/SVG 原始样本」的约束下）：
      * JPEG / PNG / GIF / WebP → 直接用扫描根目录里的真实文件
      * BMP / TIFF              → 由真实图片转换而来（已获授权），不生成任何新图片内容
      * SVG / PDF / ICO         → 用本机现成的真实文件（site-packages 内）
      * PSD / AI(EPS)           → 无样本，只能报告降级策略
    """
    hr("阶段 2 · 格式矩阵实测")
    sp = r"E:\Develop\python\Lib\site-packages"
    os.makedirs(sp_dir, exist_ok=True)

    samples: dict[str, str] = {}

    # 扫描根目录里的真实文件，按扩展名各取一个
    if os.path.isdir(root):
        want = {".jpeg", ".jpg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".ico"}
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                ext = os.path.splitext(name)[1].lower()
                if ext in want and ext not in samples:
                    samples[ext] = os.path.join(dirpath, name)
            if want <= set(samples):
                break

    # 由真实图片转换出 BMP / TIFF（授权范围内）
    src = samples.get(".jpeg") or samples.get(".jpg") or samples.get(".png")
    if src:
        try:
            from PIL import Image

            with Image.open(src) as im:
                rgb = im.convert("RGB")
                for ext in (".bmp", ".tiff"):
                    p = os.path.join(sp_dir, "converted_from_real" + ext)
                    rgb.save(p)
                    samples[ext] = p
            print(f"  转换样本来源: {os.path.basename(src)} -> BMP / TIFF（授权范围内）")
        except Exception as e:  # noqa: BLE001
            print(f"  ! 转换失败: {e}")

    # 本机现成的真实文件兜底
    for ext, pats in (
        (".svg", ("*.svg",)),
        (".pdf", ("*.pdf",)),
        (".ico", ("*.ico",)),
        (".tif", ("*.tif", "*.tiff")),
        (".bmp", ("*.bmp",)),
    ):
        if ext in samples:
            continue
        hits = _find_real(pats, (sp,), limit=1)
        if hits:
            samples[ext] = hits[0]

    rows = []
    for ext in (".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif",
                ".svg", ".ico", ".pdf"):
        p = samples.get(ext)
        if not p:
            rows.append({"ext": ext, "status": "无样本", "meta": "", "thumb": ""})
            continue
        meta = D.probe_meta(p, ext)
        arr, backend = D.load_small(p, 128, ext)
        rows.append({
            "ext": ext,
            "status": "OK" if (meta and arr is not None) else "FAIL",
            "meta": f"{meta.width}x{meta.height} via {meta.backend}" if meta else "元数据失败",
            "thumb": f"{arr.shape[1]}x{arr.shape[0]} via {backend}" if arr is not None else "缩略图失败",
            "sample": os.path.basename(p),
        })

    for r in rows:
        if r["status"] == "无样本":
            print(f"  {r['ext']:<7} —  无样本")
        else:
            mark = "OK  " if r["status"] == "OK" else "FAIL"
            print(f"  {r['ext']:<7} {mark} {r['meta']:<28} | {r['thumb']}")

    print("\n  无样本格式的降级策略：")
    print("    .psd / .psb  → 自研 8BPS 头解析取尺寸；只参与文件级与尺寸级比对")
    print("    .ai (PDF 型) → 走 pypdfium2 渲染（与 .pdf 同一容器，路径已实测）")
    print("    .ai (EPS 型) → 自研 %%BoundingBox 解析，仅取尺寸")
    return rows


# ================================================================ 阶段 3-6

def stage3_scan(root: str, timings: Timings):
    hr("阶段 3 · 目录遍历")
    t0 = time.perf_counter()
    entries = list(S.walk(root))
    timings.add("S0 遍历", time.perf_counter() - t0)
    total = sum(e.size for e in entries)
    print(f"  文件 {len(entries):,} 个 | 合计 {human_bytes(total)}")
    exts: dict[str, int] = {}
    for e in entries:
        key = e.ext or "(无扩展名)"
        exts[key] = exts.get(key, 0) + 1
    print(f"  扩展名分布: {sorted(exts.items(), key=lambda kv: -kv[1])}")

    hl = S.find_hardlink_groups(entries)
    hl_idx: set[int] = set()
    if hl:
        by_path = {e.path: i for i, e in enumerate(entries)}
        for paths in hl.values():
            for p in paths:
                hl_idx.add(by_path[p])
    print(f"  硬链接组 {len(hl)} 个（{len(hl_idx)} 个路径，默认保护，不计入可删除集合）")
    return entries, hl_idx


def stage4_metadata(entries, gate, workers, timings: Timings) -> None:
    hr("阶段 4 · 元数据提取（只读文件头，不解码像素）")

    def work(e: S.FileEntry):
        if not e.ext:
            e.ext = D.sniff_format(e.path)
        m = D.probe_meta(e.path, e.ext)
        if m:
            e.width, e.height, e.bands = m.width, m.height, m.bands
            e.fmt, e.meta_backend, e.meta_note = m.fmt, m.backend, m.note
            e.frames, e.animated = m.frames, m.animated
        return e

    t0 = time.perf_counter()
    map_concurrent(entries, work, lambda e: 64 * 1024, workers, gate, "元数据")
    timings.add("S1 元数据", time.perf_counter() - t0)

    ok = sum(1 for e in entries if e.width > 0)
    backends: dict[str, int] = {}
    for e in entries:
        backends[e.meta_backend] = backends.get(e.meta_backend, 0) + 1
    print(f"  成功 {ok:,}/{len(entries):,} | 后端分布 {sorted(backends.items(), key=lambda kv: -kv[1])}")

    big = sorted(entries, key=lambda e: -e.pixels)[:3]
    for e in big:
        print(f"  最大分辨率: {e.width}x{e.height} ({e.pixels/1e6:.1f} MP) "
              f"{human_bytes(e.size)} {os.path.basename(e.path)}")


def stage5_exact(entries, hl_idx, gate, workers, timings: Timings):
    hr("阶段 5 · 精确去重（分桶 → 短指纹 → 全量哈希）")
    buckets = S.bucket_by_size(entries)
    cand_buckets = {s: v for s, v in buckets.items() if len(v) > 1}
    cand = [e for v in cand_buckets.values() for e in v]
    print(f"  大小分桶: {len(buckets):,} 桶 → 候选桶 {len(cand_buckets):,} | "
          f"候选文件 {len(cand):,}/{len(entries):,}（排除 {100*(1-len(cand)/max(1,len(entries))):.1f}%）")

    t0 = time.perf_counter()
    map_concurrent(cand, lambda e: setattr(e, "quick", F.quick_hash(e.path)) or e,
                   lambda e: 512 * 1024, workers, gate, "短指纹")
    timings.add("S3 短指纹", time.perf_counter() - t0)

    qb: dict[tuple[int, bytes], list[S.FileEntry]] = {}
    for e in cand:
        if e.quick is None:
            continue
        qb.setdefault((e.size, e.quick), []).append(e)
    after_quick = [e for v in qb.values() if len(v) > 1 for e in v]
    print(f"  短指纹后候选: {len(after_quick):,}（再排除 {100*(1-len(after_quick)/max(1,len(cand))):.1f}%）")

    t0 = time.perf_counter()
    map_concurrent(after_quick, lambda e: setattr(e, "full", F.full_hash(e.path)) or e,
                   lambda e: 1024 * 1024, workers, gate, "全量哈希")
    timings.add("S4 全量哈希", time.perf_counter() - t0)

    groups = G.exact_groups(entries, hardlink_protected=hl_idx)
    print(f"  精确重复组: {len(groups):,}")
    return groups


def stage6_perceptual(entries, gate, workers, timings: Timings, *,
                      exclude: set[int] | None = None):
    hr("阶段 6 · 感知特征（缩略图 → pHash / dHash / 直方图）")

    def work(e: S.FileEntry):
        if e.width <= 0:
            e.feat_note = "无元数据，跳过"
            return e
        arr, backend = D.load_small(e.path, 256, e.ext, e.pixels)
        f = F.extract(arr, backend, e.meta_note)
        e.phash, e.dhash, e.hist = f.phash, f.dhash, f.hist
        e.feat_ok, e.feat_note, e.thumb_backend = f.ok, f.note, backend
        return e

    t0 = time.perf_counter()
    map_concurrent(entries, work, lambda e: D.estimate_decode_bytes(D.Meta(e.width, e.height), 256),
                   workers, gate, "感知特征")
    timings.add("S5 感知特征", time.perf_counter() - t0)

    ok = sum(1 for e in entries if e.feat_ok)
    backs: dict[str, int] = {}
    for e in entries:
        if e.feat_ok:
            backs[e.thumb_backend] = backs.get(e.thumb_backend, 0) + 1
    print(f"  成功 {ok:,}/{len(entries):,} | 解码后端分布 {sorted(backs.items(), key=lambda kv: -kv[1])}")

    t0 = time.perf_counter()
    groups = G.perceptual_groups(entries, exclude=exclude)
    timings.add("S6 聚类", time.perf_counter() - t0)
    print(f"  感知相似组: {len(groups):,}")
    return groups


# ================================================================ 报告

def summarize_groups(entries, groups, kind: str) -> dict:
    reclaim = 0
    for g in groups:
        keep = G.recommend_keeper(entries, g)
        for i in g:
            if i != keep:
                reclaim += entries[i].size
    return {
        "kind": kind,
        "groups": len(groups),
        "files": sum(len(g) for g in groups),
        "reclaim_bytes": reclaim,
        "max_group": max((len(g) for g in groups), default=0),
    }


def stage7_semantic(entries, gate, workers, timings: Timings, threshold: float,
                    *, exclude: set[int] | None = None):
    """语义相似度（M6）：CLIP 图像嵌入 + 余弦聚类。

    这是全项目唯一需要模型的功能，默认关闭。
    onnxruntime 与 numpy 走延迟导入：关闭语义时完全不加载，
    不拖慢启动，也不让"没装模型"的用户付出任何代价。
    """
    from imgdedup_core import embed as EMB  # 延迟导入

    hr("阶段 7 · 语义相似度（CLIP）")
    # `ClipEncoder.load()` 不会联网：它服务于 GUI：缺模型时直接
    # 返回 False，下载由界面显式发起（带进度对话框）。见它的实现说明。
    # 这个 CLI 是开发工具，联网取模型反而方便，所以在这里显式补一次。
    # 进度会打在控制台上，不是那种"卡住不动"的静默下载。
    try:
        EMB.ensure_model(quiet=False)
    except Exception as e:  # noqa: BLE001
        print(f"  取模型失败：{type(e).__name__}: {e}")

    enc = EMB.ClipEncoder(verbose=True)
    if not enc.load():
        print("  CLIP 不可用（缺 onnxruntime 或模型），跳过本阶段")
        return [], EMB.EmbedResult(backend="none", note="unavailable")

    # 1)+2) 分块「解码 → 编码 → 释放」。
    # 必须分块：224x224x3 每张 147 KB，1 万张全留就是 1.4 GB，会撑爆内存预算。
    # 嵌入本身只占 2 KB/张（512 x float32），1 万张才 20 MB，可以全留。
    CHUNK = 1024
    prog = Progress("语义编码", len(entries), every=max(1, len(entries) // 8))
    parts: list = []
    t_dec = t_enc = 0.0
    for c0 in range(0, len(entries), CHUNK):
        chunk = entries[c0:c0 + CHUNK]
        holder: dict[int, object] = {}

        def work(e, _h=holder):
            if e.width <= 0:
                return e
            arr, _be = D.load_small(e.path, 224, e.ext, e.pixels)
            if arr is not None:
                _h[id(e)] = arr
            return e

        t0 = time.perf_counter()
        map_concurrent(chunk, work,
                       lambda e: D.estimate_decode_bytes(D.Meta(e.width, e.height), 224),
                       workers, gate, "语义预处理")
        t_dec += time.perf_counter() - t0

        usable = [e for e in chunk if id(e) in holder]
        if usable:
            # ONNX session 内部已多线程，这里串行喂 batch，
            # 比"多个 Python 线程各跑 batch=1"更能吃满 SIMD。
            t0 = time.perf_counter()
            r = EMB.encode_all(enc, [holder[id(e)] for e in usable], batch=16,
                               on_progress=prog.tick)
            t_enc += time.perf_counter() - t0
            if not r.ok:
                print(f"  编码失败: {r.note}")
                return [], r
            for e, v in zip(usable, r.vectors):
                e.embed = v
                e.embed_ok = True
                e.embed_note = "clip-onnx"
            parts.append(r)
        holder.clear()  # 立刻释放本块缩略图

    timings.add("S7 语义预处理", t_dec)
    timings.add("S7 语义编码", t_enc)
    res = EMB.concat_results(parts)

    # 3) 余弦聚类 + 组规模护栏
    t0 = time.perf_counter()
    groups, oversized = EMB.cosine_groups(entries, res.vectors, threshold=threshold,
                                          exclude=exclude)
    timings.add("S7 语义聚类", time.perf_counter() - t0)

    print(f"  嵌入 {res.vectors.shape[0]:,} 张 × {res.vectors.shape[1]} 维"
          f" | 阈值 {threshold:.2f} | 相似组 {len(groups):,}")
    if oversized:
        mx = max(len(g) for g in oversized)
        print(f"  ! 组规模护栏拦下 {len(oversized)} 个超大连通分量（最大 {mx:,} 张）")
        print("    这些是「聚类」不是「重复组」，不允许批量删除 —— "
              "语义去重最危险的就是过合并")
        print(f"    本图库实测阈值拐点在 0.97，当前 {threshold:.2f} 偏低")
    return groups, res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ImgDedup 开发期完整流程")
    ap.add_argument("--root", default=r"E:\1\test")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--no-catalog", action="store_true")
    ap.add_argument("--semantic", action="store_true",
                    help="启用 M6 语义相似度（需 onnxruntime + CLIP 模型，首次会下载 85MB）")
    ap.add_argument("--sim-threshold", type=float, default=0.97,
                    help="语义聚类余弦阈值，默认 0.97。实测该值必须按图库调："
                         "高自相似度图库（同题材）需要 0.97 以上，"
                         "题材分散的照片库 0.90–0.92 即可")
    args = ap.parse_args(argv)

    t_all = time.perf_counter()
    timings = Timings()
    workers = args.workers or cpu_budget(0.75)
    budget = default_memory_budget()

    hr("ImgDedup 开发期完整流程")
    print(f"  数据源   : {args.root}")
    print(f"  工作线程 : {workers}")
    print(f"  内存预算 : {human_bytes(budget)}")
    print(f"  临时目录 : {SC.scratch_root()}")
    print(f"  语义层   : {'启用' if args.semantic else '关闭（--semantic 开启）'}")
    gate = MemGate(budget)

    stage1_capability()
    fmt_rows = stage2_format_matrix(str(SC.scratch_root() / "formats"), args.root)

    entries, hl_idx = stage3_scan(args.root, timings)
    if not entries:
        print("  没有扫描到文件，结束。")
        return 1

    stage4_metadata(entries, gate, workers, timings)
    exact = stage5_exact(entries, hl_idx, gate, workers, timings)
    # 每个文件只进"最强"的那一类（精确 > 感知 > 语义）：字节相同的图必然也满足
    # 感知判定，不排除就会让同一批文件同时出现在两类里、"可释放"被算两遍。
    grouped = {i for g in exact for i in g}
    perc = stage6_perceptual(entries, gate, workers, timings, exclude=grouped)
    grouped |= {i for g in perc for i in g}

    sem, sem_res = ([], None)
    if args.semantic:
        sem, sem_res = stage7_semantic(entries, gate, workers, timings,
                                       args.sim_threshold, exclude=grouped)

    hr("阶段 8 · 汇总")
    s_exact = summarize_groups(entries, exact, "exact")
    s_perc = summarize_groups(entries, perc, "perceptual")
    s_sem = summarize_groups(entries, sem, "semantic") if sem else None

    print(f"  精确重复 : {s_exact['groups']:,} 组 / {s_exact['files']:,} 文件 "
          f"→ 可释放 {human_bytes(s_exact['reclaim_bytes'])}")
    print(f"  感知相似 : {s_perc['groups']:,} 组 / {s_perc['files']:,} 文件 "
          f"→ 可释放 {human_bytes(s_perc['reclaim_bytes'])}")
    if s_sem:
        covered = set()
        for g in perc:
            covered.update(g)
        gain = [g for g in sem if any(i not in covered for i in g)]
        gain_files = sum(len(g) for g in gain)
        print(f"  语义相似 : {s_sem['groups']:,} 组 / {s_sem['files']:,} 文件 "
              f"→ 可释放 {human_bytes(s_sem['reclaim_bytes'])}")
        print(f"            其中 {len(gain):,} 组 / {gain_files:,} 文件是"
              f"感知层没发现的（语义层的净增益）")
    print(f"  最大单组 : 精确 {s_exact['max_group']} 张 / 感知 {s_perc['max_group']} 张"
          + (f" / 语义 {s_sem['max_group']} 张" if s_sem else ""))
    print(f"  内存闸门峰值: {human_bytes(gate.peak)} / 预算 {human_bytes(budget)}")

    print("\n  分组样例（精确）：")
    for g in sorted(exact, key=len, reverse=True)[:3]:
        keep = G.recommend_keeper(entries, g)
        print(f"    {len(g):>3} 张 · 保留 {os.path.basename(entries[keep].path)} "
              f"({G.keeper_reason(entries, g, keep)})")
        for i in g[:3]:
            mark = "KEEP" if i == keep else "    "
            print(f"          [{mark}] {human_bytes(entries[i].size):>10}  "
                  f"{os.path.relpath(entries[i].path, args.root)}")

    print("\n  分组样例（感知）：")
    for g in sorted(perc, key=len, reverse=True)[:3]:
        keep = G.recommend_keeper(entries, g)
        sizes = {f"{entries[i].width}x{entries[i].height}" for i in g}
        print(f"    {len(g):>3} 张 · 尺寸 {sorted(sizes)[:4]} · 保留 {os.path.basename(entries[keep].path)}")

    if sem:
        print("\n  分组样例（语义，取感知层未覆盖的）：")
        covered = set()
        for g in perc:
            covered.update(g)
        gain = [g for g in sem if any(i not in covered for i in g)]
        for g in sorted(gain, key=len, reverse=True)[:3]:
            keep = G.recommend_keeper(entries, g)
            print(f"    {len(g):>3} 张 · 保留 {os.path.basename(entries[keep].path)}")
            for i in g[:4]:
                mark = "KEEP" if i == keep else "    "
                print(f"          [{mark}] {entries[i].width}x{entries[i].height:<6} "
                      f"{human_bytes(entries[i].size):>10}  "
                      f"{os.path.relpath(entries[i].path, args.root)}")

    # ---- 持久化 ----
    db_path = str(SC.scratch_path("catalog.db"))
    if not args.no_catalog:
        cat = C.Catalog(db_path)
        scan_id = cat.begin_scan(args.root, {"workers": workers, "budget": budget,
                                             "semantic": args.semantic})
        cat.insert_files(scan_id, entries)
        for kind, grp in (("exact", exact), ("perceptual", perc), ("semantic", sem)):
            for g in grp:
                keep = G.recommend_keeper(entries, g)
                reclaim = sum(entries[i].size for i in g if i != keep)
                cat.insert_group(scan_id, kind, g, None, g.index(keep), reclaim)
        cat.end_scan(scan_id)
        n_f = cat.conn.execute("SELECT COUNT(*) FROM files WHERE scan_id=?",
                               (scan_id,)).fetchone()[0]
        n_g = cat.conn.execute("SELECT COUNT(*) FROM groups WHERE scan_id=?",
                               (scan_id,)).fetchone()[0]
        print(f"\n  catalog: {db_path} | 本次 files={n_f:,} groups={n_g:,}"
              f" | 库内累计 files={cat.count('files'):,} groups={cat.count('groups'):,}")
        cat.close()

    # ---- 计时分解 ----
    hr("耗时分解")
    for name, secs in timings.items():
        bar = "#" * int(40 * secs / max(timings.total(), 1e-9))
        print(f"  {name:<14} {human_secs(secs):>10}  {bar}")
    print(f"  {'合计':<14} {human_secs(timings.total()):>10}")
    wall = time.perf_counter() - t_all
    print(f"  {'墙钟':<14} {human_secs(wall):>10}")

    # ---- 报告落盘 ----
    report = {
        "root": args.root,
        "workers": workers,
        "memory_budget": budget,
        "memory_peak": gate.peak,
        "backends": D.BACKENDS.summary(),
        "format_matrix": fmt_rows,
        "files": len(entries),
        "total_bytes": sum(e.size for e in entries),
        "hardlink_groups": len(hl_idx),
        "exact": s_exact,
        "perceptual": s_perc,
        "semantic": s_sem,
        "semantic_threshold": args.sim_threshold if args.semantic else None,
        "semantic_backend": sem_res.backend if sem_res else None,
        "timings": dict(timings.items()),
        "wall_seconds": wall,
    }
    rp = SC.scratch_path("report.json")
    with open(rp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    print(f"\n  报告: {rp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
