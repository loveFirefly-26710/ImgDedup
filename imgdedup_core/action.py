"""路径 A：用户删除重复图片。

与路径 B（`scratch.py`，开发测试垃圾真删）物理隔离：
本模块永不真删，只走「回收站」或「用户指定目录」，并且每次操作都写
manifest、都能一键撤销。两者不共享任何函数。

核心不变量（写在代码里，不靠调用方自觉）：
  1. 每个重复组至少保留 1 张，禁止整组清空；
  2. 待删项必须位于本次扫描根之下；
  3. 硬链接（同一 inode 的多个路径）不参与删除；
  4. 精确重复组在删除前做逐字节确认（第三道闸，把哈希碰撞风险归零）；
  5. 还原前校验 hash，目标已存在时报告冲突而不是覆盖。

设计依据见 PLAN.md §8.2。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

MANIFEST_VERSION = 1

#: 用户在「删除确认对话框」上的选择，持久化到 settings 表
SETTING_MODE = "delete.mode"
SETTING_TARGET = "delete.target_dir"
SETTING_REMEMBER = "delete.remember"

MODE_RECYCLE = "recycle"
MODE_MOVE = "move"

DEFAULT_MOVE_DIRNAME = "_ImgDedup_Removed"


# ================================================================ ULID

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """生成一个 ULID（时间有序，便于按名字排序即按时间排序）。"""
    ts = int(time.time() * 1000)
    rnd = int.from_bytes(os.urandom(10), "big")

    def enc(v: int, n: int) -> str:
        out = []
        for _ in range(n):
            out.append(_CROCKFORD[v & 31])
            v >>= 5
        return "".join(reversed(out))

    return enc(ts, 10) + enc(rnd, 16)


# ================================================================ 决策

@dataclass
class Decision:
    """删除方式的选择（对应确认对话框）。"""

    mode: str = MODE_RECYCLE
    target_dir: str = ""
    remember: bool = False

    def describe(self) -> str:
        if self.mode == MODE_MOVE:
            return f"移动到指定目录 {self.target_dir}"
        return "移到回收站"


def default_target_dir(root: str) -> str:
    """指定目录的默认值：放在扫描根的同级，避免被下一次扫描扫进去。"""
    root = os.path.abspath(root)
    parent = os.path.dirname(root) or root
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(parent, DEFAULT_MOVE_DIRNAME, stamp)


def load_decision(getter: Callable[[str, object], object]) -> Optional[Decision]:
    """从 settings 读回"不再提示"记住的选择。返回 None 表示仍应弹窗。"""
    if not getter(SETTING_REMEMBER, False):
        return None
    mode = str(getter(SETTING_MODE, MODE_RECYCLE) or MODE_RECYCLE)
    if mode not in (MODE_RECYCLE, MODE_MOVE):
        return None
    return Decision(mode=mode,
                    target_dir=str(getter(SETTING_TARGET, "") or ""),
                    remember=True)


def save_decision(setter: Callable[[str, object], None], decision: Decision) -> None:
    setter(SETTING_MODE, decision.mode)
    setter(SETTING_TARGET, decision.target_dir if decision.mode == MODE_MOVE else "")
    setter(SETTING_REMEMBER, bool(decision.remember))


def clear_decision(setter: Callable[[str, object], None]) -> None:
    """设置页里的「重新启用提示」。"""
    setter(SETTING_REMEMBER, False)


# ================================================================ 计划

@dataclass
class PlanItem:
    path: str
    size: int
    group: int
    reason: str = ""


@dataclass
class Plan:
    root: str
    kind: str
    items: list[PlanItem] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    @property
    def files(self) -> int:
        return len(self.items)

    @property
    def bytes(self) -> int:
        return sum(i.size for i in self.items)


def plan_deletions(entries, groups: Iterable[list[int]], *, root: str, kind: str,
                   user_keep: Optional[dict[int, int]] = None,
                   hardlink_protected: Optional[set[int]] = None,
                   verify_bytes: bool = True) -> Plan:
    """把「分组 + 用户裁决」转成待删清单，并执行不变量检查。

    user_keep: {组序号: 用户指定保留的 entry 下标}，覆盖自动推荐。
    verify_bytes: 精确重复组在删除前逐字节确认（第三道闸）。
    """
    # 延迟导入：`feature` 会拉起 cv2 + numpy（导入是秒级的），而 GUI 的启动
    # 路径上只要碰到 action 就会 import 本模块。放在函数内可避免拖慢首屏。
    from . import feature as F
    from . import group as G

    plan = Plan(root=os.path.abspath(root), kind=kind)
    protected = hardlink_protected or set()
    user_keep = user_keep or {}

    for gi, group in enumerate(groups):
        members = [i for i in group if i not in protected]
        if len(members) < 2:
            plan.blocked.append(f"组 #{gi}：可用成员不足 2 个，跳过")
            continue

        keep = user_keep.get(gi)
        if keep is None or keep not in members:
            keep = G.recommend_keeper(entries, members)

        # 不变量 1：每组至少保留 1 张
        victims = [i for i in members if i != keep]
        if not victims:
            plan.blocked.append(f"组 #{gi}：没有可删除成员（保留下不变量）")
            continue

        if verify_bytes and kind == "exact":
            keep_path = entries[keep].path
            bad = [i for i in victims if not F.byte_identical(entries[i].path, keep_path)]
            if bad:
                plan.blocked.append(
                    f"组 #{gi}：{len(bad)} 个候选与保留项并非逐字节相同，已整体拦下")
                continue

        for i in victims:
            e = entries[i]
            # 不变量 2：必须在扫描根之下
            if not _under_root(e.path, plan.root):
                plan.blocked.append(f"组 #{gi}：{e.path} 不在扫描根之下，已拦下")
                continue
            plan.items.append(PlanItem(path=e.path, size=e.size, group=gi,
                                       reason=f"组 #{gi} 的非保留项"))
    return plan


def _under_root(path: str, root: str) -> bool:
    """路径是否在 root 之下。用 normcase+normpath 比较，不直接用字符串比。"""
    p = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    r = os.path.normcase(os.path.normpath(os.path.abspath(root)))
    return p == r or p.startswith(r + os.sep)


def plan_from_paths(paths_with_sizes, *, root: str, kind: str = "ui") -> Plan:
    """从「用户已经逐项裁决好的 (path, size) 清单」构造 Plan。

    GUI 侧的点选不需要再走一遍自动推荐，但不变量 2（必须在扫描根之下）
    仍必须生效，所以统一在这里收口，而不是让界面自己拼 Plan。
    """
    plan = Plan(root=os.path.abspath(root), kind=kind)
    seen: set[str] = set()
    for path, size in paths_with_sizes:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        if not _under_root(path, root):
            plan.blocked.append(f"{path} 不在扫描根之下，已拦下")
            continue
        plan.items.append(PlanItem(path=path, size=int(size), group=0,
                                   reason="用户裁决"))
    return plan


# ================================================================ 执行

@dataclass
class OpResult:
    manifest: str = ""
    mode: str = MODE_RECYCLE
    ok: int = 0
    failed: int = 0
    bytes: int = 0
    items: list[dict] = field(default_factory=list)

    def describe(self) -> str:
        return (f"{self.mode}：成功 {self.ok} / 失败 {self.failed}，"
                f"共 {self.bytes / 1024**2:.1f} MB")


def _to_recycle(path: str) -> tuple[bool, str]:
    """移到系统回收站。

    send2trash 存在"成功却报错"的情况（本机实测过 FileNotFoundError）。
    所以判定式是操作后目标是否还存在，而不是"有没有抛异常"。
    """
    try:
        from send2trash import send2trash
    except ImportError:
        return False, "send2trash 未安装"

    err = ""
    try:
        send2trash(path)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    if os.path.exists(path):
        return False, err or "调用未报错但文件仍在原处"
    return True, f"（send2trash 报错但文件确已移走：{err}）" if err else ""


def _to_dir(path: str, root: str, dest_root: str) -> tuple[bool, str, str]:
    """移动到指定目录，保留相对原扫描根的目录结构。"""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return False, "无法计算相对路径", ""
    if rel.startswith(".."):
        return False, "不在扫描根之下", ""
    dest = os.path.join(dest_root, rel)
    if os.path.exists(dest):
        return False, f"目标已存在，拒绝覆盖：{dest}", ""
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(path, dest)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", ""
    if os.path.exists(path):
        return False, "移动后原路径仍存在", ""
    return True, "", dest


def execute(plan: Plan, decision: Decision, *, manifest_dir: str,
            dry_run: bool = False, on_progress: Optional[Callable[[int], None]] = None) -> OpResult:
    """执行删除。只走回收站或指定目录，永不真删。

    dry_run=True 时只生成清单不落盘，用于执行前预演。
    """
    if decision.mode == MODE_MOVE:
        if not decision.target_dir:
            raise ValueError("选择「移动到指定目录」时必须给出 target_dir")
        # 预演阶段不得创建任何东西。
        # 实测踩过：dry_run 也把目标目录建了出来，违背「预演只读」的承诺。
        if not dry_run:
            os.makedirs(decision.target_dir, exist_ok=True)

    manifest_id = new_ulid()
    manifest_path = os.path.join(manifest_dir, f"{manifest_id}.json")
    res = OpResult(manifest=manifest_path, mode=decision.mode)

    for item in plan.items:
        rec = {"src": item.path, "dst": "", "size": item.size,
               "group": item.group, "status": "pending", "note": ""}

        if dry_run:
            rec["status"] = "dry-run"
            res.ok += 1
            res.bytes += item.size
            res.items.append(rec)
            if on_progress:
                on_progress(1)
            continue

        if not os.path.exists(item.path):
            # 文件在"计划"和"执行"之间被人删掉了。这不是失败：没删到就是没删到，
            # 计进 failed 会让状态栏报出"失败 N"这种吓人但没意义的数字。
            # manifest 里仍然记着 status="skipped" 与原因，要查能查到。
            rec["status"] = "skipped"
            rec["note"] = "文件已不存在"
            res.items.append(rec)
            if on_progress:
                on_progress(1)
            continue

        if decision.mode == MODE_RECYCLE:
            good, note = _to_recycle(item.path)
            rec["dst"] = "<回收站>"
        else:
            good, note, dest = _to_dir(item.path, plan.root, decision.target_dir)
            rec["dst"] = dest
        rec["note"] = note
        rec["status"] = "ok" if good else "failed"
        if good:
            res.ok += 1
            res.bytes += item.size
        else:
            res.failed += 1
        res.items.append(rec)
        if on_progress:
            on_progress(1)

    payload = {
        "version": MANIFEST_VERSION,
        "id": manifest_id,
        "ts": time.time(),
        "mode": decision.mode,
        "target_dir": decision.target_dir,
        "root": plan.root,
        "kind": plan.kind,
        "dry_run": dry_run,
        "totals": {"files": res.ok, "bytes": res.bytes,
                   "failed": res.failed, "planned": len(plan.items)},
        "blocked": plan.blocked,
        "items": res.items,
    }
    os.makedirs(manifest_dir, exist_ok=True)
    tmp = manifest_path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())          # manifest 必须先落盘，再认为操作完成
    os.replace(tmp, manifest_path)
    return res


# ================================================================ 回收站定位

def _bin_for(path: str) -> Optional[Path]:
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return None
    rb = Path(drive + os.sep) / "$Recycle.Bin"
    return rb if rb.is_dir() else None


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def build_bin_index(volume_path: str) -> dict[str, Path]:
    """扫描某个卷的回收站，返回 {原始路径(规范化): $R 实体文件}。

    为什么要建索引而不是逐个查：撤销一批 268 个文件时，若每查一个都重扫
    一遍回收站（本机约 5 千条 $I），就是 268 x 5000 次文件读取：实测不可用。

    $I 格式（Win10 v2）：偏移 8 文件大小 / 16 删除时间 / 24 名称长度 /
    28 起 UTF-16LE 原始路径。
    """
    rb = _bin_for(volume_path)
    idx: dict[str, Path] = {}
    if rb is None:
        return idx
    for sid in rb.iterdir():
        if not sid.is_dir():
            continue
        try:
            names = os.listdir(sid)
        except OSError:
            continue
        for name in names:
            if not name.startswith("$I"):
                continue
            ip = sid / name
            try:
                with open(ip, "rb") as fh:
                    raw = fh.read(8192)
                if len(raw) < 28:
                    continue
                nlen = struct.unpack("<I", raw[24:28])[0]
                if not (0 < nlen <= 4096):
                    continue
                orig = raw[28:28 + nlen * 2].decode(
                    "utf-16-le", "replace").replace("\x00", "")
            except Exception:  # noqa: BLE001
                continue
            rp = sid / ("$R" + name[2:])
            if rp.exists():
                idx[_norm_path(orig)] = rp
    return idx


def find_in_recycle_bin(original: str) -> Optional[Path]:
    """按原始路径定位回收站里的 $R 实体文件（单次查询，内部建一次索引）。"""
    return build_bin_index(original).get(_norm_path(original))


# ================================================================ 撤销

@dataclass
class UndoResult:
    restored: int = 0
    skipped: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def undo(manifest_path: str, *, on_progress: Optional[Callable[[int], None]] = None) -> UndoResult:
    """按 manifest 逆操作还原。

    冲突时报告而不是覆盖：原路径已有文件就跳过并记笔记。
    还原成功后会回写 manifest 的 undone 标记，避免重复还原。
    """
    with open(manifest_path, encoding="utf-8") as fh:
        m = json.load(fh)
    res = UndoResult()

    if m.get("dry_run"):
        res.notes.append("这是一次预演记录，没有实际删除，无需还原")
        return res
    if m.get("undone"):
        res.notes.append("该记录已还原过")
        return res

    # 回收站索引按卷惰性建立、整批复用。
    bin_idx: dict[str, dict[str, Path]] = {}

    def _bin_lookup(p: str) -> Optional[Path]:
        drive = os.path.splitdrive(os.path.abspath(p))[0].lower()
        if drive not in bin_idx:
            bin_idx[drive] = build_bin_index(p)
        return bin_idx[drive].get(_norm_path(p))

    for rec in m.get("items", []):
        if rec.get("status") != "ok":
            res.skipped += 1
            continue
        src, dst = rec["src"], rec.get("dst", "")
        if os.path.exists(src):
            res.skipped += 1
            res.notes.append(f"冲突：原路径已存在文件，跳过 {src}")
            if on_progress:
                on_progress(1)
            continue

        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            if m.get("mode") == MODE_RECYCLE:
                rp = _bin_lookup(src)
                if rp is None:
                    res.failed += 1
                    res.notes.append(f"回收站里找不到：{src}")
                    if on_progress:
                        on_progress(1)
                    continue
                shutil.move(str(rp), src)
                # 顺手清掉配对的 $I 元数据
                ip = rp.parent / ("$I" + rp.name[2:])
                try:
                    os.chmod(ip, stat.S_IWRITE)
                    os.unlink(ip)
                except OSError:
                    pass
            else:
                if not dst or not os.path.exists(dst):
                    res.failed += 1
                    res.notes.append(f"备份位置找不到：{dst}")
                    if on_progress:
                        on_progress(1)
                    continue
                shutil.move(dst, src)
        except Exception as e:  # noqa: BLE001
            res.failed += 1
            res.notes.append(f"还原失败 {src}：{type(e).__name__}: {e}")
        else:
            if os.path.exists(src):
                res.restored += 1
            else:
                res.failed += 1
                res.notes.append(f"还原后原路径仍不存在：{src}")
        if on_progress:
            on_progress(1)

    m["undone"] = res.failed == 0
    m["undone_ts"] = time.time()
    m["undo_result"] = asdict(res)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=2)
    return res
