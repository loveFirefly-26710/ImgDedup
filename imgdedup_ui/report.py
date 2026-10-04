"""导出扫描结果为 CSV / HTML。

报告里的类别名比筛选面板的长（面板要短标签，报告要能自解释），所以这里的
`KIND_LABEL` 与 `window.KIND_LABEL` 是两份、不是同一份。"""

from __future__ import annotations

import csv
import html
import os
import time
from typing import Iterable

KIND_LABEL = {
    "exact": "字节完全相同",
    "perceptual": "感知相似",
    "semantic": "语义相似",
    "cluster": "聚类（过合并，不参与批量删除）",
}



def kind_text(row) -> str:
    base = KIND_LABEL.get(row.kind, row.kind)
    extra = row.label
    if row.oversized or not extra or extra in (row.kind, base):
        return base
    return f"{base}（{extra}）"


def _mb(n: float) -> str:
    return f"{n / 1024**2:.1f} MB"


def rows_to_records(rows: Iterable) -> list[dict]:
    out = []
    for gi, row in enumerate(rows):
        for pos, item in enumerate(row.items):
            out.append({
                "group": gi,
                "kind": row.kind,
                "kind_label": kind_text(row),
                "oversized": row.oversized,
                "decision": item.decision,
                "is_keeper": row.is_kept(pos),
                "path": item.path,
                "name": os.path.basename(item.path),
                "size": item.size,
                "width": item.width,
                "height": item.height,
                "fmt": item.fmt,
            })
    return out


def export_csv(rows, path: str) -> int:
    recs = rows_to_records(rows)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["组号", "类型", "裁决", "保留项", "文件名", "路径",
                    "大小(字节)", "宽", "高", "格式"])
        for r in recs:
            w.writerow([r["group"], r["kind_label"],
                        {"keep": "保留", "delete": "待删"}.get(r["decision"], "未裁决"),
                        "是" if r["is_keeper"] else "",
                        r["name"], r["path"], r["size"], r["width"], r["height"],
                        r["fmt"]])
    return len(recs)


def export_html(rows, meta: dict | None, path: str) -> int:
    meta = meta or {}
    rows = list(rows)
    live = [r for r in rows if r.deletable]
    to_delete = sum(1 for r in live for p, i in enumerate(r.items)
                    if not r.is_kept(p) and i.decision == "delete")
    reclaim = sum(r.reclaim for r in live)
    generated = time.strftime("%Y-%m-%d %H:%M:%S")

    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        "<title>ImgDedup 去重报告</title>",
        "<style>",
        "body{font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;",
        "margin:32px;color:#2C2C2A;background:#FAFAF8}",
        "h1{font-size:20px;margin:0 0 4px}",
        ".sub{color:#888780;margin-bottom:20px}",
        ".cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}",
        ".card{background:#fff;border:1px solid #E3E3E0;border-radius:8px;",
        "padding:12px 16px;min-width:140px}",
        ".card b{display:block;font-size:22px;font-weight:500}",
        ".card span{color:#888780;font-size:12px}",
        "table{border-collapse:collapse;width:100%;background:#fff;",
        "border:1px solid #E3E3E0;border-radius:8px;overflow:hidden}",
        "th,td{border-bottom:1px solid #EFEFEC;padding:7px 10px;text-align:left;",
        "font-size:13px;vertical-align:top}",
        "th{background:#F7F7F5;font-weight:500;color:#5F5E5A}",
        "tr.keeptd{background:#F3FBF7}",
        "tr.del td{color:#A32D2D}",
        ".tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:12px}",
        ".t-exact{background:#E1F5EE;color:#0F6E56}",
        ".t-perceptual{background:#FAEEDA;color:#854F0B}",
        ".t-semantic{background:#E6F1FB;color:#185FA5}",
        ".t-cluster{background:#FCEBEB;color:#A32D2D}",
        "code{font-family:Consolas,monospace;font-size:12px;color:#5F5E5A}",
        "</style></head><body>",
        "<h1>ImgDedup 去重报告</h1>",
        f'<div class="sub">生成于 {generated} · 数据源 <code>'
        f'{html.escape(str(meta.get("root", "—")))}</code></div>',
        '<div class="cards">',
        f'<div class="card"><b>{meta.get("files", 0):,}</b><span>扫描文件</span></div>',
        f'<div class="card"><b>{len(rows):,}</b><span>分组</span></div>',
        f'<div class="card"><b>{to_delete:,}</b><span>待删</span></div>',
        f'<div class="card"><b>{_mb(reclaim)}</b><span>预计释放</span></div>',
        "</div>",
        "<table><thead><tr><th>组</th><th>类型</th><th>裁决</th><th>尺寸</th>"
        "<th>大小</th><th>文件</th></tr></thead><tbody>",
    ]

    for gi, row in enumerate(rows):
        for pos, item in enumerate(row.items):
            is_keep = row.is_kept(pos)
            if row.oversized:
                cls, decision = "", "需人工确认"
            else:
                cls = ("keeptd" if is_keep
                       else ("del" if item.decision == "delete" else ""))
                decision = "保留" if is_keep else (
                    "待删" if item.decision == "delete" else "未裁决")
            dims = f"{item.width}×{item.height}" if item.width else "—"
            parts.append(
                f'<tr class="{cls}"><td>{gi}</td>'
                f'<td><span class="tag t-{row.kind}">'
                f'{html.escape(kind_text(row))}</span></td>'
                f"<td>{decision}</td><td>{dims}</td><td>{_mb(item.size)}</td>"
                f'<td><code>{html.escape(item.path)}</code></td></tr>')

    parts.append("</tbody></table>")
    if any(r.oversized for r in rows):
        parts.append('<p class="sub">注意：标为「聚类（过合并）」的分组不参与批量删除，'
                     "需要逐张人工确认。</p>")
    parts.append("</body></html>")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    return len(rows)
