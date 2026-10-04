"""校验 `requirements.txt` 与 `THIRD_PARTY_NOTICES.md` 的组件表是否一致。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_REQ = PROJECT_ROOT / "requirements.txt"
DEFAULT_NOTICES = PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"

DERIVED = {
    "libvips": "由 pyvips-binary 带进来的动态库，不是 pip 包",
}

EXTERNAL_PREFIX = ("cv2", "clip 模型")

MIN_ROWS = 8


def parse_requirements(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-r"):
            continue
        names.append(re.split(r"[=<>!;]", s)[0].strip())
    return names


def parse_notices(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if set(s) <= set("|- "):
            continue
        first = s.strip("|").split("|")[0].strip()
        if not first or first == "组件":
            continue
        first = re.sub(r"[（(].*?[)）]", "", first).strip().lower()
        if first:
            names.append(first)
    return names


def check(req_path: Path = DEFAULT_REQ,
          notices_path: Path = DEFAULT_NOTICES) -> list[str]:
    problems: list[str] = []

    pkgs = parse_requirements(req_path)
    rows = parse_notices(notices_path)

    if len(rows) < MIN_ROWS:
        problems.append(
            f"组件表只解析出 {len(rows)} 行（至少应有 {MIN_ROWS} 行）—— "
            f"表格结构变了，或者解析正则坏了：{notices_path.name}")
        return problems
    if not pkgs:
        problems.append(f"没从 {req_path.name} 解析出任何依赖")

    lowered = [r.lower() for r in rows]

    for p in pkgs:
        key = p.lower()
        if not any(key == r or key in r for r in lowered):
            problems.append(
                f"{req_path.name} 里的 {p} 在 {notices_path.name} 的组件表里没有对应行")

    for r in rows:
        if r in DERIVED or r.startswith(EXTERNAL_PREFIX):
            continue
        if not any(r in p.lower() or p.lower() in r for p in pkgs):
            problems.append(
                f"{notices_path.name} 的组件表里有 {r}，"
                f"但它既不在 {req_path.name} 里，也不是白名单里的派生项或外置组件")

    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="校验依赖声明与第三方组件表是否一致")
    ap.add_argument("--requirements", type=Path, default=DEFAULT_REQ,
                    help="依赖清单路径")
    ap.add_argument("--notices", type=Path, default=DEFAULT_NOTICES,
                    help="第三方组件声明路径")
    args = ap.parse_args(argv)

    for p in (args.requirements, args.notices):
        if not p.is_file():
            print(f"  找不到 {p}")
            return 2

    print("=" * 66)
    print("  依赖声明 ↔ 第三方组件表")
    print("=" * 66)

    try:
        problems = check(args.requirements, args.notices)
    except Exception as e:
        print(f"  解析失败：{type(e).__name__}: {e}")
        return 2

    if not problems:
        n_req = len(parse_requirements(args.requirements))
        n_rows = len(parse_notices(args.notices))
        print(f"  {args.requirements.name}: {n_req} 个依赖")
        print(f"  {args.notices.name}: {n_rows} 行组件表")
        print("\n  结论：一致 ✅")
        return 0

    for p in problems:
        print(f"  ! {p}")
    print(f"\n  结论：{len(problems)} 处漂移 ❌")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
