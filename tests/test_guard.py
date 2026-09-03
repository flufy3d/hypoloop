"""「只有验证者能改文件」这条不变量的测试。

这是整套系统里唯一一条**机制性**保证，所以它必须有测试：光靠提示词里那句「你不许
改文件」，模型哪天心血来潮就破了，而且破了没人知道。
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from hypoloop.guard import (ReadOnlyGuard, WorktreeChanged, fingerprint,
                            is_git_repo, run_git)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root)] + list(args),
                   capture_output=True, check=False)


class GitRepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@t.t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")

    def tearDown(self):
        self._tmp.cleanup()


class TestFingerprint(GitRepoCase):
    def test_repo_detected(self):
        self.assertTrue(is_git_repo(self.root))

    def test_stable_when_nothing_changes(self):
        self.assertEqual(fingerprint(self.root), fingerprint(self.root))

    def test_tracked_edit_changes_it(self):
        before = fingerprint(self.root)
        (self.root / "a.txt").write_text("hello world\n", encoding="utf-8")
        self.assertNotEqual(before, fingerprint(self.root))

    def test_new_untracked_file_changes_it(self):
        before = fingerprint(self.root)
        (self.root / "b.txt").write_text("new\n", encoding="utf-8")
        self.assertNotEqual(before, fingerprint(self.root))

    def test_untracked_content_edit_changes_it(self):
        # git status --porcelain 只列未跟踪文件的**路径**，内容改了它一字不变。
        # 只哈希 status 输出就会漏掉这种改动 —— 这正是要单独哈希内容的原因。
        (self.root / "b.txt").write_text("v1\n", encoding="utf-8")
        before = fingerprint(self.root)
        (self.root / "b.txt").write_text("v2\n", encoding="utf-8")
        self.assertNotEqual(before, fingerprint(self.root))

    def test_deletion_changes_it(self):
        before = fingerprint(self.root)
        (self.root / "a.txt").unlink()
        self.assertNotEqual(before, fingerprint(self.root))


class TestReadOnlyGuard(GitRepoCase):
    def test_passes_when_nothing_written(self):
        with ReadOnlyGuard(self.root, "假设者"):
            (self.root / "a.txt").read_text(encoding="utf-8")

    def test_raises_when_worktree_touched(self):
        with self.assertRaises(WorktreeChanged) as ctx:
            with ReadOnlyGuard(self.root, "假设者"):
                (self.root / "sneaky.js").write_text("boom\n", encoding="utf-8")
        self.assertIn("假设者", str(ctx.exception))
        self.assertIn("sneaky.js", str(ctx.exception))

    def test_disabled_guard_allows_writes(self):
        with ReadOnlyGuard(self.root, "验证者", enabled=False):
            (self.root / "ok.js").write_text("fine\n", encoding="utf-8")

    def test_does_not_mask_an_inner_exception(self):
        # 里面本来就炸了的时候，别用 WorktreeChanged 把真正的错因盖掉
        with self.assertRaises(ValueError):
            with ReadOnlyGuard(self.root, "假设者"):
                (self.root / "x.js").write_text("x\n", encoding="utf-8")
                raise ValueError("真正的错在这")


class TestNonGitFallback(unittest.TestCase):
    def test_walk_fingerprint_detects_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("1", encoding="utf-8")
            before = fingerprint(root)
            self.assertTrue(before.startswith("walk:"))
            (root / "b.txt").write_text("2", encoding="utf-8")
            self.assertNotEqual(before, fingerprint(root))

    def test_run_git_never_raises_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_git(Path(tmp), "status", "--porcelain")
            self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
