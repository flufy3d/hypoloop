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
