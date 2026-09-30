"""发布链路（workflow + 产物验收脚本）的回归测试。

## 为什么要有这个文件

`ci.yml` / `release.yml` / `dev/check_artifacts.py` 这三样东西的共同点是：
**它们坏掉的时候，单元测试一条都不会红。**

- workflow 写错一个 `permissions`，CI 照跑，只是悄悄多给了写权限；
- release 少跑一道产物门禁，坏包照样发出去，直到用户装上才发现；
- 产物验收脚本漏判一条红线，86 MB 的 cv2 就跟着安装包一起发出去。

所以这里用和 `tests/test_packaging.py` 同一套办法：把配置当文本读进来，
把"不能退化"的那几条钉死。

## 为什么不用 PyYAML 解析

`requirements-dev.txt` 里没有 PyYAML，测试不该为了解析一份 yml 去加依赖。
而且 GitHub Actions 的 yml 是 YAML 的超集（`on:` 在 YAML 1.1 里会被解析成
布尔 `True`），真拿 yaml 解析反而要额外绕一圈。断言直接锚在源码文本上，
和 `test_packaging.py` 一致。
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJ = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJ))

CI = PROJ / ".github" / "workflows" / "ci.yml"
RELEASE = PROJ / ".github" / "workflows" / "release.yml"
CHECK_ARTIFACTS = PROJ / "dev" / "check_artifacts.py"


def read(p: Path) -> str:
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def strip_comments(text: str) -> str:
    """去掉整行注释，只留真正会被执行的配置。

    这一步不能省：本仓库的 workflow 里注释比配置还多，直接 `assertIn`
    一个词很可能命中的是注释 —— 那就成了「把配置删掉、只留注释，测试照样绿」
    的伪测试（`test_packaging.py` 里 `icon=str(ICON_ICO)` 那条断言就是被
    "命中了注释里的同一个词"坑过的）。
    """
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def jobs_of(text: str) -> dict[str, str]:
    """把 workflow 文本按 job 切开：{job 名: 该 job 的文本块}。

    只认「顶格 `jobs:` 之下、缩进恰好两格」的行当 job 名。
    step 名缩进六格且带 `- `，不会误命中。
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "jobs:" and not ln.startswith(" "):
            start = i + 1
            break
    if start is None:
        return {}

    blocks: dict[str, list[str]] = {}
    cur: str | None = None
    for ln in lines[start:]:
        if ln and not ln.startswith(" ") and not ln.startswith("#"):
            break                       # 回到顶层，jobs 段结束
        m = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", ln)
        if m:
            cur = m.group(1)
            blocks[cur] = []
            continue
        if cur is not None:
            blocks[cur].append(ln)
    return {k: "\n".join(v) for k, v in blocks.items()}


class TestWorkflowShape(unittest.TestCase):
    def test_both_workflows_exist(self):
        self.assertTrue(CI.is_file(), "缺少 .github/workflows/ci.yml")
        self.assertTrue(RELEASE.is_file(), "缺少 .github/workflows/release.yml")

    def test_every_job_has_a_hard_timeout(self):
        """每个 job 都要有 `timeout-minutes`。

        这不是洁癖：本仓库真出过「测试挂在模态对话框上、一路挂到 job 超时」
        的事故（见 `dev/check_fresh_clone.py` 的 docstring）。没有这个字段时
        GitHub 给的是 6 小时默认值，日志里只剩一行超时，看不出卡在哪。
        """
        for path in (CI, RELEASE):
            jobs = jobs_of(strip_comments(read(path)))
            self.assertTrue(jobs, f"{path.name} 里没解析出任何 job")
            for name, body in jobs.items():
                self.assertIn(
                    "timeout-minutes", body,
                    f"{path.name} 的 job「{name}」没有 timeout-minutes")

    def test_actions_are_pinned_to_a_major_tag(self):
        """`uses:` 不许指到会移动的分支（@main / @master）。"""
        for path in (CI, RELEASE):
            for m in re.finditer(r"uses:\s*(\S+)", strip_comments(read(path))):
                ref = m.group(1)
                self.assertRegex(
                    ref, r"@v\d+(\.\d+)*$",
                    f"{path.name} 里的 {ref} 没钉到 major tag")


class TestCiGate(unittest.TestCase):
    def test_ci_is_read_only(self):
        """CI 只读仓库。不写的话默认权限跟随仓库设置，对 fork 的 PR 偏大。"""
        src = strip_comments(read(CI))
        self.assertIn("permissions:\n  contents: read\n", src,
                      "ci.yml 没有收窄到 contents: read")

    def test_ci_runs_the_three_gates(self):
        src = strip_comments(read(CI))
        self.assertIn("unittest discover", src, "CI 没跑单元测试")
        self.assertIn("dev/check_no_network.py", src, "CI 没跑断网门禁")
        self.assertIn("dev/check_artifacts.py", src, "CI 没跑产物自检")
        self.assertIn("dev/check_frozen.py", src, "CI 没跑归档符号校验")

    def test_ci_does_not_take_shortcuts_on_docs(self):
        """不许给文档加 `paths-ignore`。

        文档、PLAN、测试项数之间是互相钉住的
        （`tests/test_packaging.py::TestDocsConsistency` 会读
        `docs/开发文档.md` 里的项数），改一个 md 就能把 CI 弄红。
        加了 paths-ignore 就等于让这类改动绕过门禁。
        """
        self.assertNotIn("paths-ignore", strip_comments(read(CI)))


class TestReleasePipeline(unittest.TestCase):
    def test_only_the_release_job_can_write(self):
        src = strip_comments(read(RELEASE))
        self.assertIn("permissions:\n  contents: read\n", src,
                      "release.yml 顶层应默认只读")
        jobs = jobs_of(src)
        self.assertIn("contents: write", jobs.get("release", ""),
                      "release job 需要 contents: write 才能发 Release")
        self.assertNotIn("contents: write", jobs.get("test", ""),
                         "test job 不该有写权限")

    def test_release_runs_the_artifact_gates(self):
        """tag 推送不触发 ci.yml，所以这三道门禁必须在 release 里自己跑。

        少了它们，一个从没走过打包冒烟的 commit 打上标签就能直接发出去。
        """
        src = strip_comments(read(RELEASE))
        for token in ("dev/make_installer.py --build",
                      "dev/check_artifacts.py",
                      "dev/check_frozen.py"):
            self.assertIn(token, src, f"release.yml 缺少「{token}」这一步")

    def test_release_asserts_tag_matches_version(self):
        """tag 与 `__version__` 不一致时必须中止。

        否则会出现「Release 页是 v1.1.0、挂的产物叫 ImgDedup-1.0.0-setup.exe」。
        """
        src = strip_comments(read(RELEASE))
        self.assertIn("imgdedup_core.__version__", src,
                      "release.yml 没有读 __version__ 做比对")
        self.assertIn('StartsWith("v$ver")', src,
                      "release.yml 没有断言 tag 以 v<版本> 开头")

    def test_release_never_invents_a_tag(self):
        """`gh release create` 在没有 `--verify-tag` 时会照着默认分支
        自己造一个 tag。手动触发填错名字就会造出一个莫名其妙的 tag。"""
        self.assertIn("--verify-tag", strip_comments(read(RELEASE)))

    def test_release_upload_is_idempotent(self):
        """重跑要能补齐附件，而不是因为「Release 已存在」直接报错。"""
        src = strip_comments(read(RELEASE))
        self.assertIn("--clobber", src, "重跑时附件没法覆盖上传")

    def test_manual_dispatch_takes_a_tag_input(self):
        """手动触发时 `github.ref_name` 是**分支名**，不能直接当 tag 用。"""
        src = strip_comments(read(RELEASE))
        self.assertIn("workflow_dispatch:", src)
        self.assertIn("RELEASE_TAG:", src, "没有把 tag 解析成一个显式变量")
        self.assertIn("inputs.tag || github.ref_name", src,
                      "手动触发没有优先取输入的 tag")

    def test_release_checks_assets_before_publishing(self):
        """上传前必须确认产物齐全。

        早先的写法是 `$assets += (Get-ChildItem ...).FullName`：
        拿不到文件时 PowerShell 会往数组里塞一个 `$null`，数组照样"非空"，
        于是 Release 建出来了、附件少一个，谁都不报错。
        """
        src = strip_comments(read(RELEASE))
        self.assertIn("缺少产物", src, "没有在发布前断言产物齐全")

    def test_release_concurrency_does_not_cancel(self):
        """两个 tag 同时推时不能互相取消：取消一个正在上传的发布，
        会留下「Release 建了、附件缺一半」的半截状态。"""
        src = strip_comments(read(RELEASE))
        self.assertIn("cancel-in-progress: false", src)


class TestBranchPushRelease(unittest.TestCase):
    """推 master 也能发版，但只在版本号变了的时候。

    形状是：`gate` 先查 `v<__version__>` 的 Release 在不在，在就把
    test / release 两个 job 整个跳过。挡住的只有"推分支"这一种情况 ——
    随手 push 的代码不该重发；推 tag 和手动触发是明确的发布意图。
    """

    def _src(self) -> str:
        return strip_comments(read(RELEASE))

    def test_branch_push_is_a_trigger(self):
        """`on.push` 上得有 master，否则推代码根本走不到这个 workflow。"""
        src = self._src()
        self.assertIn('branches: ["master"]', src,
                      "release.yml 没有监听 master 分支")

    def test_gate_job_gates_both_expensive_jobs(self):
        """`gate` 得真挡在前面，而且 test / release 两个都要听它的。

        只听一个的话另一个照样跑满：test 25 分钟，release 还要下 170 MB。
        """
        jobs = jobs_of(self._src())
        self.assertIn("gate", jobs, "release.yml 里没有 gate job")
        self.assertIn("publish", jobs["gate"], "gate 没把结论输出成 publish")
        for name in ("test", "release"):
            self.assertIn("needs.gate.outputs.publish", jobs[name],
                          f"{name} job 没读 gate 的结论")
        self.assertIn("needs: gate", jobs["test"], "test job 没依赖 gate")
        self.assertIn("needs: [gate, test]", jobs["release"],
                      "release job 没依赖 gate 和 test")

    def test_only_branch_pushes_are_skipped(self):
        """能挡的只有分支推送。

        推 tag 和手动触发是明确的发布意图，一律构建 —— 半截的 Release
        正是靠重跑这两条路把附件补齐的。所以 gate 必须同时看事件名和
        ref 类型，只看其中一个都会把这两条路一起挡掉。
        """
        gate = jobs_of(self._src())["gate"]
        self.assertIn("$env:GITHUB_EVENT_NAME -eq 'push'", gate,
                      "gate 没看事件类型")
        self.assertIn("$env:GITHUB_REF_TYPE -eq 'branch'", gate,
                      "gate 没区分 tag / 分支")

    def test_branch_path_derives_the_tag_from_version(self):
        """推分支时没有 tag 可用，只能从 `__version__` 推出来。

        而且只能推一处：release job 直接用 gate 的结论。两边各推一遍，
        改了一边就变成两套判据了。
        """
        src = self._src()
        self.assertIn('$tag = "v$ver"', src,
                      "推分支时没有从 __version__ 推导 tag")
        self.assertEqual(src.count('$tag = "v$ver"'), 1,
                         "推导 tag 的规则出现了不止一次")
        self.assertIn("needs.gate.outputs.tag", jobs_of(src)["release"],
                      "release job 没有用 gate 定好的 tag")

    def test_release_pushes_the_tag_before_creating_it(self):
        """`--verify-tag` 要求 tag 已在远端，所以新建的得自己推上去。

        少了这一步，推分支那条路会在 `gh release create` 上直接中止。
        """
        src = self._src()
        self.assertIn("git push origin $tag", src,
                      "新建的 tag 没有推上去，--verify-tag 会中止")
        self.assertIn("--verify-tag", src)

    def test_existing_tag_must_point_at_this_commit(self):
        """远端已有同名 tag 时，要确认它指向本次提交。

        不确认的话，会把"用本次代码构建的产物"挂到一个指向旧提交的 tag
        上，Release 页和源码就对不上了 —— 那正是 `check_frozen.py` 在防的
        那类事，只是这次发生在 tag 上。
        """
        src = self._src()
        self.assertIn('git rev-list -n 1 "refs/tags/$tag"', src,
                      "没有核对已有的 tag 指向哪次提交")
        self.assertIn("git rev-parse HEAD", src, "没有取本次提交的 SHA")

    def test_probe_does_not_leak_its_exit_code(self):
        """探针故意的非 0 退出码不能变成整步的退出码。

        GitHub 的 pwsh 处理器在脚本末尾会 `exit $LASTEXITCODE`，而
        `gh release view` 在 Release 不存在时返回 1 —— 那是预期结果。
        真踩过一次：日志里两个 Write-Host 都打印了、publish / tag 也都设上
        了，这一步还是 `Process completed with exit code 1`，后面两个 job
        全被挡在外面。
        """
        gate = jobs_of(self._src())["gate"]
        probe = gate.index("gh release view")
        self.assertGreater(
            gate.find("$LASTEXITCODE = 0", probe), probe,
            "探针跑完没把 $LASTEXITCODE 归零，它的退出码会漏成整步的退出码")


class TestArtifactChecks(unittest.TestCase):
    """`dev/check_artifacts.py` 的红线扫描。"""

    def _fake_onedir(self, td: Path) -> Path:
        root = td / "ImgDedup"
        (root / "_internal").mkdir(parents=True)
        (root / "ImgDedup.exe").write_bytes(b"MZ" + b"\0" * 64)
        return root

    def test_script_exists(self):
        self.assertTrue(CHECK_ARTIFACTS.is_file(),
                        "缺少 dev/check_artifacts.py")

    def test_clean_tree_reports_nothing(self):
        from dev.check_artifacts import forbidden_entries

        with tempfile.TemporaryDirectory() as td:
            root = self._fake_onedir(Path(td))
            self.assertEqual(forbidden_entries(root), [])

    def test_bundled_model_is_caught(self):
        """模型是外置组件：有人把它加回 `datas` 就必须被看到。"""
        from dev.check_artifacts import forbidden_entries

        with tempfile.TemporaryDirectory() as td:
            root = self._fake_onedir(Path(td))
            (root / "_internal" / "models").mkdir()
            bad = forbidden_entries(root)
            self.assertTrue(any("models" in b for b in bad), bad)

    def test_bundled_cv2_is_caught(self):
        from dev.check_artifacts import forbidden_entries

        with tempfile.TemporaryDirectory() as td:
            root = self._fake_onedir(Path(td))
            (root / "_internal" / "cv2.pyd").write_bytes(b"x")
            bad = forbidden_entries(root)
            self.assertTrue(any("cv2" in b for b in bad), bad)

    def test_agpl_component_is_caught(self):
        """许可红线：产物里不许出现 AGPL 组件。"""
        from dev.check_artifacts import forbidden_entries

        with tempfile.TemporaryDirectory() as td:
            root = self._fake_onedir(Path(td))
            (root / "_internal" / "fitz").mkdir()
            (root / "_internal" / "_mupdf.cp313-win_amd64.pyd").write_bytes(b"x")
            bad = forbidden_entries(root)
            self.assertTrue(any("fitz" in b for b in bad), bad)
            self.assertTrue(any("_mupdf" in b for b in bad), bad)

    def test_missing_internal_is_reported(self):
        """产物不是 onedir 形态时要说话，不能静默通过。"""
        from dev.check_artifacts import forbidden_entries

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "ImgDedup"
            root.mkdir()
            bad = forbidden_entries(root)
            self.assertEqual(len(bad), 1, bad)
            self.assertIn("_internal", bad[0])

    def test_missing_exe_exits_nonzero(self):
        """没产物就必须退非 0：否则 CI 那一步会绿着过去。"""
        from dev.check_artifacts import main

        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope" / "ImgDedup.exe"
            self.assertEqual(main([str(missing), "--no-selftest"]), 1)

    def test_no_selftest_does_not_execute_the_exe(self):
        """`--no-selftest` 只做静态扫描：里面的 exe 是假的（几个字节），
        真去执行它会失败。这条同时证明"静态扫描不依赖能跑起来的产物"。"""
        from dev.check_artifacts import main

        with tempfile.TemporaryDirectory() as td:
            root = self._fake_onedir(Path(td))
            self.assertEqual(
                main([str(root / "ImgDedup.exe"), "--no-selftest"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
