"""删除路径 A：永不真删。

只走「移到回收站」或「移动到指定目录」两条路。每次操作写一份 manifest
（含每个文件的源路径、去向、大小），撤销时按它还原。"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import struct
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

MANIFEST_VERSION = 1

SETTING_MODE = "delete.mode"
SETTING_TARGET = "delete.target_dir"
SETTING_REMEMBER = "delete.remember"

MODE_RECYCLE = "recycle"
MODE_MOVE = "move"

DEFAULT_MOVE_DIRNAME = "_ImgDedup_Removed"



_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    ts = int(time.time() * 1000)
    rnd = int.from_bytes(os.urandom(10), "big")

    def enc(v: int, n: int) -> str:
        out = []
        for _ in range(n):
            out.append(_CROCKFORD[v & 31])
            v >>= 5
        return "".join(reversed(out))

    return enc(ts, 10) + enc(rnd, 16)



@dataclass
class Decision:

    mode: str = MODE_RECYCLE
    target_dir: str = ""
    remember: bool = False


def default_target_dir(root: str) -> str:
    root = os.path.abspath(root)
    parent = os.path.dirname(root) or root
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(parent, DEFAULT_MOVE_DIRNAME, stamp)


def load_decision(getter: Callable[[str, object], object]) -> Optional[Decision]:
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


@dataclass
class PlanItem:
    path: str
    size: int
    group: int


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


def _under_root(path: str, root: str) -> bool:
    p = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    r = os.path.normcase(os.path.normpath(os.path.abspath(root)))
    return p == r or p.startswith(r + os.sep)


def plan_from_paths(paths_with_sizes, *, root: str, kind: str = "ui") -> Plan:
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
        plan.items.append(PlanItem(path=path, size=int(size), group=0))
    return plan



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
    try:
        from send2trash import send2trash
    except ImportError:
        return False, "send2trash 未安装"

    err = ""
    try:
        send2trash(path)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    if os.path.exists(path):
        return False, err or "调用未报错但文件仍在原处"
    return True, f"（send2trash 报错但文件确已移走：{err}）" if err else ""


def _to_dir(path: str, root: str, dest_root: str) -> tuple[bool, str, str]:
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
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", ""
    if os.path.exists(path):
        return False, "移动后原路径仍存在", ""
    return True, "", dest


def execute(plan: Plan, decision: Decision, *, manifest_dir: str,
            dry_run: bool = False, on_progress: Optional[Callable[[int], None]] = None) -> OpResult:
    if decision.mode == MODE_MOVE:
        if not decision.target_dir:
            raise ValueError("选择「移动到指定目录」时必须给出 target_dir")
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
        os.fsync(fh.fileno())
    os.replace(tmp, manifest_path)
    return res



def _bin_for(path: str) -> Optional[Path]:
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return None
    rb = Path(drive + os.sep) / "$Recycle.Bin"
    return rb if rb.is_dir() else None


def _long_path(p: str) -> str:
    """把路径里「存在的部分」换成 8.3 长名，剩下的一段原样拼回去。

    回收站的 `$I` 元数据里记的是长名路径，而调用方给的可能是短名
    （`C:\\Users\\LIGHTN~1\\...`）。两边不归一，撤销就会报「回收站里找不到」。

    文件这时候已经被删了，所以最后一段可能不存在 —— 那就往上找到第一个
    存在的祖先，换成长名再拼回来。GetLongPathNameW 对不存在的路径返回 0，
    正好可以用作探测。
    """
    if sys.platform != "win32":
        return p
    p = os.path.abspath(p)
    head, tail = p, ""
    while True:
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetLongPathNameW(head, buf, 32768):
            return os.path.join(buf.value, tail) if tail else buf.value
        parent, name = os.path.split(head)
        if not name or parent == head:
            return p
        tail = os.path.join(name, tail) if tail else name
        head = parent


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(_long_path(p)))


def build_bin_index(volume_path: str) -> dict[str, Path]:
    """扫回收站，返回 {原路径(已归一化): $R 数据文件} 的索引。

    回收站里每个被删文件是一对：`$I` 存元数据（原路径 / 删除时间 / 大小），
    `$R` 存内容，两者只差第二个字符。要还原就得靠 `$I` 里的原路径去配 `$R`。

    `$I` 的布局（Win10+，版本 2）：8 字节头 + 8 字节文件大小 + 8 字节删除时间
    (FILETIME) + 4 字节路径长度 + 路径(UTF-16LE)。路径长度在偏移 24 处。
    """
    rb = _bin_for(volume_path)
    idx: dict[str, Path] = {}
    if rb is None:
        return idx
    # 一个卷的回收站下，每个 SID 一个子目录
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
                # 路径长度是 4 字节小端，之后紧跟路径本身
                nlen = struct.unpack("<I", raw[24:28])[0]
                if not (0 < nlen <= 4096):
                    continue
                orig = raw[28:28 + nlen * 2].decode(
                    "utf-16-le", "replace").replace("\x00", "")
            except Exception:
                continue
            # $Ixxxx 与 $Rxxxx 是同一后缀的一对，只有第二个字符不同
            rp = sid / ("$R" + name[2:])
            if rp.exists():
                idx[_norm_path(orig)] = rp
    return idx


@dataclass
class UndoResult:
    restored: int = 0
    skipped: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def undo(manifest_path: str, *, on_progress: Optional[Callable[[int], None]] = None) -> UndoResult:
    with open(manifest_path, encoding="utf-8") as fh:
        m = json.load(fh)
    res = UndoResult()

    # 预演记录和已还原过的记录都不用动，直接说明原因返回
    if m.get("dry_run"):
        res.notes.append("这是一次预演记录，没有实际删除，无需还原")
        return res
    if m.get("undone"):
        res.notes.append("该记录已还原过")
        return res

    # 按盘符缓存回收站索引：一次 undo 往往涉及成千上万个文件，
    # 每个文件都重扫一遍回收站会慢到不可用。
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
                # 把 $R 搬回去之后，对应的 $I 元数据要清掉，否则回收站里会
                # 留下一对「有记录、没内容」的孤儿项。$I 带只读属性，先改权限。
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
        except Exception as e:
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
