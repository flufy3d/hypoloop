"""提交守卫的测试。

验证者被要求「取证工具和临时产物不许留在目标仓库里」，但那是提示词里的一句请求。
`--commit` 原本走的是 `git add -A`，验证者要是留下 node_modules 或一堆截图，会被
一起提交进目标仓库的历史 —— 用在别人的仓库上时这是不可接受的。所以提交前得挑一遍。
"""

import unittest

from hypoloop.loop import _changed_paths, _is_junk


class TestJunkFilter(unittest.TestCase):
    def test_source_changes_are_not_junk(self):
        for p in ("js/game.js", "index.html", "style.css",
                  "vendor/addons/postprocessing/EffectComposer.js",
                  "src/main.py", "README.md", "icon.svg"):
            self.assertFalse(_is_junk(p), p)

    def test_tooling_dirs_are_junk_at_any_depth(self):
        for p in ("node_modules/playwright/index.js", "tools/node_modules/x/y.js",
                  ".venv/lib/site.py", "__pycache__/mod.pyc",
                  "playwright-report/index.html", "test-results/a/trace.zip"):
            self.assertTrue(_is_junk(p), p)

    def test_evidence_artifacts_are_junk(self):
        for p in ("before-menu.png", "after-menu.png", "shot-1.png",
                  "baseline-metrics.json", "server.log", "tmp_probe.js",
                  "screenshot.png", "hypoloop-check.js"):
            self.assertTrue(_is_junk(p), p)

    def test_a_legitimate_image_is_not_junk(self):
        # 项目自己的图标是正当改动，别因为它是 png 就误杀
        for p in ("icon-192.png", "assets/logo.png", "textures/road.png"):
            self.assertFalse(_is_junk(p), p)

    def test_windows_separators(self):
        self.assertTrue(_is_junk(r"node_modules\playwright\cli.js"))
        self.assertFalse(_is_junk(r"js\game.js"))


class TestPorcelainParsing(unittest.TestCase):
    def test_parses_status_lines(self):
        import hypoloop.loop as loop

        sample = (" M js/game.js\n"
                  "?? before-menu.png\n"
                  "A  src/new.js\n"
                  'R  old.js -> js/renamed.js\n'
                  '?? "js/带空格 的文件.js"\n')
        orig = loop.run_git
        loop.run_git = lambda root, *a: (0, sample)
        try:
            paths = _changed_paths("whatever")
        finally:
            loop.run_git = orig
        self.assertIn("js/game.js", paths)
        self.assertIn("before-menu.png", paths)
        self.assertIn("src/new.js", paths)
        # 重命名要取新名字，拿旧名字去 git add 会报 pathspec 不存在
        self.assertIn("js/renamed.js", paths)
        self.assertNotIn("old.js -> js/renamed.js", paths)
        self.assertIn("js/带空格 的文件.js", paths)

    def test_git_failure_yields_empty_not_crash(self):
        import hypoloop.loop as loop

        orig = loop.run_git
        loop.run_git = lambda root, *a: (128, "not a repo")
        try:
            self.assertEqual(_changed_paths("whatever"), [])
        finally:
            loop.run_git = orig


if __name__ == "__main__":
    unittest.main()


class TestSingleCommitPerRun(unittest.TestCase):
    """跑了几轮是这套系统内部的事，不该泄进目标仓库的历史。

    原本是一轮一个提交，于是别人的仓库里会平白多出「hypoloop 第 1 轮 / 第 2 轮」
    两条记录 —— 对仓库的主人来说这是实现细节噪音。现在整个 run 收一个提交，
    每轮的裁决放进提交信息正文，信息不丢。
    """

    def _capture(self, rounds, changed=("js/game.js",)):
        import hypoloop.loop as loop

        calls = []
        orig_git, orig_changed = loop.run_git, loop._changed_paths
        loop._changed_paths = lambda t: list(changed)
        loop.run_git = lambda root, *a: (calls.append(a), (0, ""))[1]
        try:
            loop._commit_run("t", "让它跑得更快", rounds, lambda s: None)
        finally:
            loop.run_git, loop._changed_paths = orig_git, orig_changed
        return calls

    def test_exactly_one_commit(self):
        rounds = [{"round": 1, "verify": {"kept": ["H1"]}},
                  {"round": 2, "verify": {"kept": ["H4"]}}]
        commits = [c for c in self._capture(rounds) if c and c[0] == "commit"]
        self.assertEqual(len(commits), 1)

    def test_subject_is_the_task_not_a_round_label(self):
        rounds = [{"round": 1, "verify": {"kept": ["H1"]}}]
        msg = [c for c in self._capture(rounds) if c[0] == "commit"][0][-1]
        self.assertTrue(msg.startswith("让它跑得更快"), msg[:60])
        self.assertNotIn("第 1 轮：让它跑得更快", msg)

    def test_round_verdicts_survive_in_the_body(self):
        rounds = [{"round": 1, "verify": {"kept": ["H1"], "reverted": ["H2", "H3"],
                                          "summary": "抬阈值把霓虹斩没了"}},
                  {"round": 2, "verify": {"kept": ["H4"]}}]
        msg = [c for c in self._capture(rounds) if c[0] == "commit"][0][-1]
        for token in ("第 1 轮", "第 2 轮", "H1", "H2", "H3", "H4",
                      "实证保留", "证伪回滚", "抬阈值把霓虹斩没了"):
            self.assertIn(token, msg)

    def test_no_commit_when_nothing_changed(self):
        calls = self._capture([{"round": 1, "verify": {}}], changed=())
        self.assertEqual([c for c in calls if c and c[0] == "commit"], [])

    def test_junk_is_never_added(self):
        rounds = [{"round": 1, "verify": {"kept": ["H1"]}}]
        calls = self._capture(rounds, changed=("js/game.js", "shot-1.png",
                                               "node_modules/pw/x.js"))
        adds = [c for c in calls if c and c[0] == "add"]
        self.assertEqual(len(adds), 1)
        self.assertIn("js/game.js", adds[0])
        self.assertNotIn("shot-1.png", adds[0])
        self.assertNotIn("node_modules/pw/x.js", adds[0])

    def test_aborted_round_still_records_why(self):
        # 中途崩了也要落地，并且在提交信息里交代清楚
        rounds = [{"round": 1, "verify": {"kept": ["H1"]}},
                  {"round": 2, "error": "工作区指纹变了：假设者动了文件"}]
        msg = [c for c in self._capture(rounds) if c[0] == "commit"][0][-1]
        self.assertIn("中止", msg)
        self.assertIn("指纹", msg)
