"""文档链接门禁：相对路径、锚点、代码围栏。

## 为什么需要

文档里的链接会**悄悄烂掉**：改了标题、挪了文件、重排了章节号，
引用它的地方不会报错，只会在有人点进去的时候发现是空的。

实测抓到过的：

- `## 两个外置组件` 的锚点是 `#两个外置组件`，而
  `.github/ISSUE_TEMPLATE/config.yml` 里写的是 `#-两个外置组件`
  —— 那个前导连字符是标题里曾经有 emoji 留下的。点进去直接到不了。
- 文档重排章节号之后，正文里的"见第 N 节"会集体漂掉。
  （这一条由人工 grep 兜着，见开发文档第 10 节的记录；机械能查的是锚点与链接。）

## GitHub 的锚点算法（最容易被写错的一条）

1. 去掉 HTML 标签、转小写；
2. 去掉所有"非字母数字 / 非空格 / 非连字符 / 非下划线"的字符
   —— 中文算字母所以保留，emoji 与标点被删掉；
3. **每个空格换成"一个"连字符，连续空格换成连续连字符（不折叠）。**

第 3 条是坑：`## 怎么拿到 / 怎么启动` 去掉 `/` 后剩下两个空格，
锚点是 `怎么拿到--怎么启动`（**双**连字符）。
用 `re.sub(r"\\s+", "-")` 会把它算成单连字符，于是**把一条正确的锚点
报成失效**，然后去"修"一个本来没错的东西 —— 误改比漏改更糟。
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
import unittest
from pathlib import Path

PROJ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJ))

#: 本仓库自己的 GitHub 地址。`.github/` 里的自引用链接（指回 README）按它识别。
REPO_URL = "https://github.com/loveFirefly-26710/ImgDedup"

#: 参与检查的文档。用 glob 而不是写死清单：新加的 md 自动进来。
def doc_files() -> list[Path]:
    out: list[Path] = []
    for pat in ("*.md", "docs/*.md", ".github/*.md",
                ".github/ISSUE_TEMPLATE/*.yml", ".github/*.yml",
                ".github/workflows/*.yml"):
        out.extend(sorted(PROJ.glob(pat)))
    return [p for p in out if p.is_file()]


def anchor_of(heading: str) -> str:
    """按 GitHub 规则算标题锚点。"""
    s = re.sub(r"<[^>]+>", "", heading.strip().lower())
    kept = []
    for ch in s:
        if ch in " -_":
            kept.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat[0] in "LN" or cat == "Lo":     # 字母 / 数字 / 汉字
            kept.append(ch)
    return "".join(kept).replace(" ", "-")    # 每个空格一个连字符，不折叠


def anchors_of(text: str) -> set[str]:
    return {anchor_of(m.group(1))
            for m in re.finditer(r"^#{1,6}\s+(.*?)\s*$", text, re.M)}


#: 文档里会出现的仓库顶层目录。只检查以它们开头的反引号路径 ——
#: 这样能自动排除 `cv2/__init__.py`（那是 cv2 包里的）和
#: `pypdfium2_raw/bindings.py` 这类**别的包**的路径。
REPO_TOP = {"imgdedup_core", "imgdedup_ui", "dev", "tests", "docs",
            "resources", ".github", ".gitlab", "build", "dist", ".scratch"}

#: 根目录下的单个文件（不带 `/`），同样会被检查
REPO_ROOT_FILES = {"imgdedup.spec", "imgdedup.iss", "README.md", "PLAN.md",
                   "THIRD_PARTY_NOTICES.md", "LICENSE", "requirements.txt",
                   "requirements-dev.txt", "启动ImgDedup.bat", ".gitignore",
                   ".gitattributes"}

#: 占位符 / 配置表达式 / 省略号，都不是真路径
PLACEHOLDER = re.compile(r"xxx|\.\.\.|=")

#: 看起来像源文件的扩展名
SRC_EXT = (".py", ".md", ".yml", ".yaml", ".txt", ".spec", ".iss", ".bat",
           ".ico", ".png", ".json", ".onnx")

PATH_TOKEN = re.compile(r"`([^`\n]+)`")


def repo_path_refs(text: str) -> list[tuple[int, str]]:
    """抽出文档里像"本仓库文件"的反引号路径，返回 [(行号, 归一化路径)]。

    收紧到"以仓库顶层目录（或根文件）开头"，是为了不误报别的包里的同名路径。
    """
    out: list[tuple[int, str]] = []
    for m in PATH_TOKEN.finditer(text):
        tok = m.group(1).strip()
        if not tok or " " in tok or len(tok) > 90:
            continue
        if PLACEHOLDER.search(tok):
            continue
        norm = tok.replace("\\", "/")
        if "/" in norm:
            if norm.split("/")[0] not in REPO_TOP:
                continue
        elif norm not in REPO_ROOT_FILES:
            continue
        if not norm.lower().endswith(SRC_EXT):
            continue
        out.append((text[:m.start()].count("\n") + 1, norm))
    return out


class TestDocLinks(unittest.TestCase):
    def setUp(self):
        self.texts = {p: p.read_text(encoding="utf-8") for p in doc_files()}
        self.anchors = {p: anchors_of(t) for p, t in self.texts.items()}

    def test_docs_are_discovered(self):
        """防止 glob 写错导致整套检查空转 —— 空集合上的断言永远绿。"""
        self.assertGreaterEqual(len(self.texts), 10,
                                "没找到足够的文档，glob 是不是写错了？")

    def test_relative_links_resolve(self):
        """相对路径链接的目标文件必须存在。"""
        bad = []
        for p, text in self.texts.items():
            for m in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
                link = m.group(1).strip()
                if link.startswith(("http://", "https://", "mailto:")):
                    continue
                target = link.split("#", 1)[0]
                if not target:
                    continue
                if not (p.parent / target).exists():
                    ln = text[:m.start()].count("\n") + 1
                    bad.append(f"{p.relative_to(PROJ)}:{ln} -> {target}")
        self.assertEqual(bad, [], "这些相对链接指向了不存在的文件：\n  "
                         + "\n  ".join(bad))

    def test_anchors_resolve(self):
        """锚点（含自引用 URL 上的）必须能对上某个标题。

        自引用那条是实测踩出来的：`.github/ISSUE_TEMPLATE/config.yml` 里的
        `...#-两个外置组件` 多了一个连字符，指向一个不存在的锚点。
        """
        bad = []
        for p, text in self.texts.items():
            # 1) markdown 链接里的 #锚点（同文件或跨文件）
            for m in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
                link = m.group(1).strip()
                if link.startswith(("http://", "https://")):
                    continue
                if "#" not in link:
                    continue
                path_part, _, frag = link.partition("#")
                if not frag:
                    continue
                ln = text[:m.start()].count("\n") + 1
                if path_part:
                    tgt = (p.parent / path_part).resolve()
                    if not tgt.exists():
                        continue          # 上面那条测试管文件不存在
                    pool = self.anchors.get(tgt)
                    if pool is None:
                        pool = anchors_of(tgt.read_text(encoding="utf-8"))
                        self.anchors[tgt] = pool
                else:
                    pool = self.anchors[p]
                if frag not in pool:
                    bad.append(f"{p.relative_to(PROJ)}:{ln} -> #{frag}")

            # 2) 裸 URL 里的自引用锚点（指回本仓库 README）
            for m in re.finditer(re.escape(REPO_URL) + r"#([^\s\"']+)", text):
                frag = m.group(1)
                ln = text[:m.start()].count("\n") + 1
                if frag not in self.anchors[PROJ / "README.md"]:
                    bad.append(f"{p.relative_to(PROJ)}:{ln} -> #{frag}")

        self.assertEqual(bad, [], "这些锚点在目标文档里找不到对应标题：\n  "
                         + "\n  ".join(bad))

    def test_code_fences_are_balanced(self):
        """围栏数必须是偶数，否则整篇后半段的渲染都会坏掉。"""
        bad = []
        for p, text in self.texts.items():
            n = len(re.findall(r"^```", text, re.M))
            if n % 2:
                bad.append(f"{p.relative_to(PROJ)}（{n} 个）")
        self.assertEqual(bad, [], "代码围栏是奇数：\n  " + "\n  ".join(bad))

    def test_anchor_rule_handles_consecutive_spaces(self):
        """锚点算法本身的自检：连续空格**不折叠**。

        这条是在测"校验器自己有没有坏"。把算法改成
        `re.sub(r"\\s+", "-")` 会让它在这里立刻失败 ——
        否则它会悄悄把正确的锚点报成失效，然后有人去改一个本来没错的引用。
        """
        self.assertEqual(anchor_of("怎么拿到 / 怎么启动"), "怎么拿到--怎么启动")
        self.assertEqual(anchor_of("两个外置组件"), "两个外置组件")
        # 有 emoji 的标题：emoji 被删掉，它后面那个空格变成前导连字符
        self.assertEqual(anchor_of("⚡ 两个外置组件"), "-两个外置组件")


    def test_referenced_repo_paths_exist(self):
        """文档里反引号写出的仓库相对路径，文件必须真的在。

        `imgdedup_core/__init__.py` 里那句 `tests/test_decoupling.py` 就是这么
        烂掉的（文件改名成 `test_guardrails.py`），当时没有任何检查发现得了。
        """
        missing = []
        for p, text in self.texts.items():
            for ln, ref in repo_path_refs(text):
                if (PROJ / ref).exists() or (p.parent / ref).exists():
                    continue
                missing.append(f"{p.relative_to(PROJ)}:{ln} -> {ref}")
        self.assertEqual(missing, [],
                         "文档引用了不存在的文件：\n  " + "\n  ".join(missing))

    def test_path_extractor_is_not_vacuous(self):
        """防止正则写错导致上面那条空转 —— 空集合上的断言永远绿。"""
        n = sum(len(repo_path_refs(t)) for t in self.texts.values())
        self.assertGreater(n, 30, f"只抽出 {n} 条路径引用，提取器是不是写错了？")

    def test_path_extractor_ignores_other_packages(self):
        """别的包里的同名路径不能误报 —— 这是收紧判据的理由。"""
        self.assertEqual(repo_path_refs("见 `cv2/__init__.py` 第 12 行"), [])
        self.assertEqual(repo_path_refs("`pypdfium2_raw/bindings.py` 找 dll"), [])
        self.assertEqual(repo_path_refs("`SetupIconFile=..\\resources\\icon.ico`"), [])
        self.assertEqual(repo_path_refs("写成 `dev/check_xxx.py`"), [])
        # 真的仓库路径要能抽出来
        self.assertEqual(repo_path_refs("见 `dev/check_frozen.py`"),
                         [(1, "dev/check_frozen.py")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
