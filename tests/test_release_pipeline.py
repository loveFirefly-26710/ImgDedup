"""两个 workflow 的不变量。

这些约束的共同点是：写错了不会让任何本地测试变红，只会在某次推送或发版时
才暴露出来（挂到 job 超时、发出半截 Release、把 171 MB 卷进 zip）。
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CI = ".github/workflows/ci.yml"
RELEASE = ".github/workflows/release.yml"


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _code(rel: str) -> str:
    """去掉整行注释后的内容。

    断言前必须先剥注释：这些 workflow 里大段注释就是在解释「为什么不用 X」，
    里面明明白白写着 X（`pull_request_target`、`unittest discover`）。
    不剥的话，两条本该守规矩的断言会一直误报。
    """
    return "\n".join(ln for ln in _read(rel).split("\n")
                      if not ln.strip().startswith("#"))


def _jobs(text: str) -> dict[str, str]:
    """按缩进粗解析 `jobs:` 段，返回 {job 名: 该 job 的正文}。

    不引入 yaml 依赖 —— 这里要断言的几条（超时、权限、步骤顺序）都是文本层面的。
    """
    lines = text.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "jobs:")
    except StopIteration:
        return {}
    out: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for ln in lines[start + 1:]:
        if ln and not ln[0].isspace():
            break
        m = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", ln)
        if m:
            if cur:
                out[cur] = "\n".join(buf)
            cur, buf = m.group(1), []
        elif cur is not None:
            buf.append(ln)
    if cur:
        out[cur] = "\n".join(buf)
    return out


class WorkflowCase(unittest.TestCase):

    rel = CI

    def setUp(self):
        self.text = _read(self.rel)
        self.jobs = _jobs(self.text)

    def test_has_jobs(self):
        self.assertTrue(self.jobs, "没解析出任何 job —— 判据自身可能坏了")


class TestBothWorkflows(unittest.TestCase):

    def test_every_job_has_a_hard_timeout(self):
        """没有 `timeout-minutes` 时 GitHub 给 6 小时默认值。

        挂在模态对话框上的测试会一路挂到 job 超时，日志里只剩一行超时。
        """
        for rel in (CI, RELEASE):
            for name, body in _jobs(_read(rel)).items():
                with self.subTest(workflow=rel, job=name):
                    self.assertIn("timeout-minutes:", body)
                    self.assertIn("runs-on:", body)

    def test_actions_are_pinned_to_a_major_version(self):
        for rel in (CI, RELEASE):
            for m in re.finditer(r"uses:\s*(\S+)", _read(rel)):
                ref = m.group(1)
                if ref.startswith("./"):
                    continue
                with self.subTest(workflow=rel, ref=ref):
                    self.assertRegex(ref, r"@v\d+$",
                                     "action 必须钉到主版本，别用 @main 这类浮动引用")

    def test_no_pull_request_target(self):
        """`pull_request_target` 会带着基础仓库的 token 去跑 fork 的代码。"""
        for rel in (CI, RELEASE):
            with self.subTest(workflow=rel):
                self.assertNotIn("pull_request_target", _code(rel))

    def test_artifact_gates_run_in_both(self):
        for rel in (CI, RELEASE):
            src = _read(rel)
            for gate in ("check_artifacts.py", "check_frozen.py"):
                with self.subTest(workflow=rel, gate=gate):
                    self.assertIn(gate, src)

    def test_stdout_encoding_is_forced_to_utf8(self):
        """Windows runner 的 Python stdio 默认是 cp1252，一句中文 print 就能把构建弄红。"""
        for rel in (CI, RELEASE):
            with self.subTest(workflow=rel):
                self.assertIn("PYTHONIOENCODING: utf-8", _read(rel))


class TestCi(WorkflowCase):

    rel = CI

    def test_jobs(self):
        self.assertEqual(set(self.jobs), {"test", "packaging", "no-network"})

    def test_concurrency_cancels_stale_runs(self):
        self.assertIn("cancel-in-progress: true", self.text)

    def test_permissions_are_read_only(self):
        self.assertIn("contents: read", self.text)

    def test_runs_the_full_suite(self):
        body = self.jobs["test"]
        self.assertIn("unittest discover", body)
        self.assertIn("-s tests", body)
        self.assertIn('-p "test_*.py"', body)

    def test_no_network_job_guards_the_suite(self):
        """这一趟证明「测试不需要网」——和 test job 那趟证明的事不一样。"""
        self.assertIn("check_no_network.py", self.jobs["no-network"])

    def test_interface_smoke_runs_offscreen(self):
        self.assertIn("--interact-test", self.text)
        self.assertIn("QT_QPA_PLATFORM: offscreen", self.text)

    def test_packaging_job_builds_with_clean(self):
        """`--clean` 要和 `dev/make_installer.py` 保持一致，否则验的不是发布那条路径。"""
        body = self.jobs["packaging"]
        self.assertIn("PyInstaller", body)
        self.assertIn("--clean", body)

    def test_packaging_job_deliberately_skips_external_components(self):
        body = self.jobs["packaging"]
        self.assertNotIn("fetch_cv2", body)
        self.assertNotIn("fetch_model", body)


class TestRelease(WorkflowCase):

    rel = RELEASE

    def test_jobs(self):
        self.assertEqual(set(self.jobs), {"gate", "test", "release"})

    def test_release_does_not_cancel_in_progress(self):
        """取消一个正在上传的发布，会留下「Release 建了、附件缺一半」的半截状态。"""
        self.assertIn("cancel-in-progress: false", self.text)

    def test_release_waits_for_the_tests(self):
        self.assertRegex(self.jobs["release"], r"needs:\s*\[gate, test\]")

    def test_tag_push_does_not_skip_the_tests(self):
        """推 tag 不会触发 ci.yml（那个只监听分支），所以这道闸必须自带。"""
        self.assertIn("unittest discover", self.jobs["test"])

    def test_gate_checks_whether_the_version_was_already_released(self):
        body = self.jobs["gate"]
        self.assertIn("gh release view", body)
        self.assertIn("publish=", body)

    def test_version_and_tag_are_cross_checked(self):
        body = self.jobs["release"]
        self.assertIn("__version__", body)
        self.assertRegex(body, r"RELEASE_TAG")

    def test_release_job_can_write_contents(self):
        self.assertIn("contents: write", self.jobs["release"])

    def test_portable_zip_is_built_before_fetching_components(self):
        """顺序反了的话，cv2 与模型会被卷进免安装版的 zip 里（多 170 MB）。"""
        portable = self.text.index("ImgDedup-portable-win64.zip")
        fetch_cv2 = self.text.index("fetch_cv2")
        self.assertLess(portable, fetch_cv2,
                        "免安装版 zip 必须在取外置组件之前打")

    def test_checksums_step_asserts_artifacts_are_present(self):
        """拿不到文件时 PowerShell 会往数组里塞 $null，数组照样「非空」。

        所以这一步必须显式检查每个产物存在，而不是只看数组长度。
        """
        self.assertIn("缺少产物", self.text)
        self.assertIn("SHA256SUMS.txt", self.text)

    def test_release_upload_is_idempotent(self):
        self.assertIn("--clobber", self.text)
        self.assertIn("--verify-tag", self.text)

    def test_tag_is_created_after_the_build(self):
        """补建 tag 放在最后，构建中途失败时不会留下「有 tag、没 Release」。"""
        build = self.text.index("make_installer.py --build")
        create_tag = self.text.index("git tag $tag")
        self.assertLess(build, create_tag)

    def test_release_notes_file_is_used(self):
        self.assertIn("release-notes.md", self.text)


if __name__ == "__main__":
    unittest.main()
