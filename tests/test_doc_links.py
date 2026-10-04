"""文档自身的一致性：相对链接能解析、锚点对得上标题、代码围栏成对。

还有一条更容易被忽略的：文档里用反引号写出来的仓库路径，得真的存在。
删代码时最容易漏的就是这一处 —— 正文改了，链接还在指向已经不存在的东西。
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "__pycache__", "audit-workspace", ".scratch", "build", "dist"}

# 反引号路径检查只认这些顶层目录 + 这些后缀，避免把命令、参数当成路径
PATH_PREFIXES = ("imgdedup_core/", "imgdedup_ui/", "dev/", "docs/", "tests/",
                 "resources/", ".github/", "extra/")
PATH_SUFFIXES = (".py", ".md", ".yml", ".yaml", ".iss", ".spec", ".txt",
                 ".json", ".ico", ".png", ".bat", ".onnx")

# 这些路径是「故意写出来但当前不存在」的，逐个说明理由
ALLOWED_MISSING = {
    "tests/",                 # 规划中的目录，正文里以目录形式出现
    "resources/models/",      # 外置模型来源，不进 git（.gitignore 里写明）
    "extra/",                 # 外置组件目录，运行期才生成
    ".workbuddy-ai/",         # 本地工作记忆，不进 git
    "audit-workspace/",       # 审计工作目录，不进 git
    "dist/", "build/", ".scratch/",   # 构建与开发期垃圾
}

# 文档里的占位示例名（不是真路径）
ALLOWED_PLACEHOLDERS = {
    "dev/check_xxx.py",       # 「加一个质量门禁」一节的示例名
}


def anchor(text: str) -> str:
    """GitHub 的锚点算法：转小写 → 去掉非字母数字空格连字符下划线 → 空格换连字符。"""
    text = text.strip().lower()
    kept = [c for c in text if c in (" ", "-", "_") or c.isalnum()]
    return "".join(kept).replace(" ", "-")


def md_files() -> list[str]:
    out = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return sorted(out)


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def headings(text: str) -> set[str]:
    out = set()
    for line in text.split("\n"):
        m = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if m:
            out.add(anchor(m.group(1)))
    return out


class TestRelativeLinks(unittest.TestCase):

    def test_every_relative_link_resolves(self):
        bad = []
        for rel in md_files():
            base = os.path.dirname(rel)
            for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", read(rel)):
                target = m.group(1).strip()
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path = target.split("#", 1)[0]
                if not path:
                    continue
                full = os.path.normpath(os.path.join(base, path))
                if not os.path.exists(os.path.join(ROOT, full)):
                    bad.append(f"{rel} -> {target}")
        self.assertEqual(bad, [], "失效的相对链接：\n  " + "\n  ".join(bad))

    def test_cross_file_anchors_match_a_heading(self):
        bad = []
        for rel in md_files():
            base = os.path.dirname(rel)
            for m in re.finditer(r"\[[^\]]*\]\(([^)]*#[^)]+)\)", read(rel)):
                target = m.group(1).strip()
                if target.startswith(("http://", "https://")):
                    continue
                path, frag = target.split("#", 1)
                if path:
                    full = os.path.normpath(os.path.join(base, path))
                    if not full.endswith(".md"):
                        continue
                    if not os.path.exists(os.path.join(ROOT, full)):
                        continue
                    text = read(full)
                else:
                    text = read(rel)
                if frag not in headings(text):
                    bad.append(f"{rel} -> {target}")
        self.assertEqual(bad, [], "锚点对不上标题：\n  " + "\n  ".join(bad))

    def test_anchor_algorithm_self_check(self):
        """判据自身也要有对照，否则「全都不匹配」和「全都没问题」长得一样。"""
        cases = {
            "## 0. 五分钟上手": "0-五分钟上手",
            "## 2. 核心引擎 `imgdedup_core`": "2-核心引擎-imgdedup_core",
            "## 9. 编码约定（硬规则）": "9-编码约定硬规则",
            "# 一、设计": "一设计",
            "## 4. `widgets.py` 的 `QColor` 常量": "4-widgetspy-的-qcolor-常量",
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                m = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
                self.assertEqual(anchor(m.group(1)), want)


class TestCodeFences(unittest.TestCase):

    def test_fences_are_paired(self):
        bad = []
        for rel in md_files():
            n = sum(1 for ln in read(rel).split("\n")
                    if ln.strip().startswith("```"))
            if n % 2:
                bad.append(f"{rel}（{n} 个围栏标记）")
        self.assertEqual(bad, [], "代码围栏没有成对：\n  " + "\n  ".join(bad))


class TestBacktickedPaths(unittest.TestCase):

    def test_referenced_repo_paths_exist(self):
        """正文里用反引号写出来的仓库路径必须真的存在。

        这是「删了文件但忘了改文档」最直接的探测器。
        """
        pattern = re.compile(r"`([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+)`")
        bad = []
        for rel in md_files():
            text = read(rel)
            # 代码块里的内容不算「正文引用」，跳过围栏内部
            lines, inside = [], False
            for ln in text.split("\n"):
                if ln.strip().startswith("```"):
                    inside = not inside
                    continue
                if not inside:
                    lines.append(ln)
            for m in pattern.finditer("\n".join(lines)):
                cand = m.group(1)
                if not cand.startswith(PATH_PREFIXES):
                    continue
                if not cand.endswith(PATH_SUFFIXES):
                    continue
                if cand in ALLOWED_PLACEHOLDERS:
                    continue
                if any(cand.startswith(a) for a in ALLOWED_MISSING):
                    continue
                if not os.path.exists(os.path.join(ROOT, cand)):
                    bad.append(f"{rel}: {cand}")
        self.assertEqual(bad, [], "文档里引用了不存在的仓库路径：\n  " + "\n  ".join(bad))


class TestNoStaleTestReferences(unittest.TestCase):

    def test_docs_do_not_cite_specific_test_files_that_do_not_exist(self):
        """文档里点名的测试文件必须真的在 tests/ 下。

        防的是「文档里的数字没人核对」。数字对不上比数字写错更糟 —— 它让人以为
        有一层保护，实际什么都没有：文档可以写着「tests/ 下 21 个文件、494 项测试」，
        而目录是空的。
        """
        pattern = re.compile(r"`?(tests/test_[A-Za-z0-9_]+\.py)")
        bad = []
        for rel in md_files():
            for m in pattern.finditer(read(rel)):
                cand = m.group(1)
                if not os.path.exists(os.path.join(ROOT, cand)):
                    bad.append(f"{rel}: {cand}")
        self.assertEqual(bad, [], "文档点名了不存在的测试文件：\n  " + "\n  ".join(bad))


class TestDocumentedNumbers(unittest.TestCase):
    """文档里写的测试模块数与断言数，必须和实际对得上。

    防的是「文档里的数字没人核对」。数字对不上比数字写错更糟 —— 它让人以为
    有一层保护，实际什么都没有：文档可以写着「`tests/` 下 21 个文件、494 项测试」，
    而 `tests/` 目录是空的。
    """

    @classmethod
    def setUpClass(cls):
        tests_dir = os.path.join(ROOT, "tests")
        cls.modules = len([f for f in os.listdir(tests_dir)
                           if f.startswith("test_") and f.endswith(".py")])
        suite = unittest.TestLoader().discover(tests_dir, pattern="test_*.py",
                                               top_level_dir=ROOT)
        cls.cases = suite.countTestCases()

    def test_counts_are_sane(self):
        # 判据自检：真数出来个 0 的话，下面的断言会「碰巧」通过或全错
        self.assertGreater(self.modules, 10)
        self.assertGreater(self.cases, 100)

    def test_readme(self):
        text = read("README.md")
        self.assertIn(f"{self.modules} 个模块", text)
        self.assertIn(f"{self.cases} 项断言", text)

    def test_dev_doc(self):
        text = read("docs/开发文档.md")
        self.assertIn(f"{self.modules} 个测试模块", text)
        self.assertIn(f"{self.cases} 项断言", text)

    def test_plan(self):
        text = read("PLAN.md")
        self.assertIn(f"{self.modules} 个模块", text)
        self.assertIn(f"{self.cases} 项断言", text)


if __name__ == "__main__":
    unittest.main()
