"""环境探测的测试。

这里守的是一条设计边界，不只是几个函数：

    环境事实（机器的属性）→ 自动探测、自动注入
    取证提示（人的叮嘱）  → --evidence-hint，默认空
    任务结论（该改什么）  → 谁都不许预先写

最后一条是最容易破的：写提示的人一顺手就会把「这个项目的 X 应该改成 Y」塞进去，
于是这一轮就没法说明假设者能自己发现问题了。所以有一个测试专门盯着自动注入的那段
不许出现项目相关的内容。
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hypoloop import environ

# 从这台机器上真实的 agy 日志里抠出来的一行，不是手写的
REAL_LINE = (
    "ERROR: logging before google.Init: E0903 17:07:50.091672     114 "
    "browser_context.go:179] failed to install playwright: could not install "
    "driver: could not install driver: error: got non 200 status code: 404 "
    "(404 Not Found) from "
    "https://playwright.azureedge.net/builds/driver/playwright-1.57.0-win32_x64.zip"
)


def _fake_home(lines=()):
    tmp = TemporaryDirectory()
    log_dir = Path(tmp.name) / environ.AGY_LOG_DIR
    log_dir.mkdir(parents=True)
    (log_dir / "cli-20260903_170750.log").write_text(
        "\n".join(lines), encoding="utf-8")
    return tmp


class TestAgyBrowserDetection(unittest.TestCase):
    def test_reads_the_real_error_line(self):
        with _fake_home([REAL_LINE]) as _:
            pass
        tmp = _fake_home(["something boring", REAL_LINE, "more noise"])
        try:
            reason = environ.agy_browser_broken(Path(tmp.name))
            self.assertIn("404", reason)
            self.assertIn("playwright-1.57.0-win32_x64.zip", reason)
            # 不要把 glog 的前缀（时间戳、go 文件行号）也抓进来
            self.assertNotIn("browser_context.go", reason)
            self.assertNotIn("google.Init", reason)
        finally:
            tmp.cleanup()

    def test_silent_when_never_happened(self):
        # 没在这台机器上观测到，就一个字都不说 —— 这是观测，不是预测
        tmp = _fake_home(["all quiet", "nothing to see"])
        try:
            self.assertEqual(environ.agy_browser_broken(Path(tmp.name)), "")
        finally:
            tmp.cleanup()

    def test_missing_log_dir_is_not_a_crash(self):
        with TemporaryDirectory() as empty:
            self.assertEqual(environ.agy_browser_broken(Path(empty)), "")


class TestBlock(unittest.TestCase):
    def test_block_is_empty_when_nothing_detected(self):
        # notes() 全空时不能吐出一个只有标题的空壳块
        orig = environ.notes
        environ.notes = lambda home=None: []
        try:
            self.assertEqual(environ.block(), "")
        finally:
            environ.notes = orig

    def test_block_mentions_the_upstream_issue(self):
        tmp = _fake_home([REAL_LINE])
        try:
            text = environ.block(Path(tmp.name))
            self.assertIn("agy", text)
            self.assertIn("issues/638", text)
            # 关键：必须说清「坏的只是 agy 内置的，不是浏览器取证这条路」，
            # 否则验证者会以为根本没法用浏览器，直接放弃取证
            self.assertIn("npm", text)
        finally:
            tmp.cleanup()

    def test_block_carries_no_project_specific_content(self):
        """自动注入的这段是**机器**的属性，不许夹带任何项目/任务的结论。

        这条是整个模块存在的理由。一旦这里开始出现「这个项目该怎么改」，
        假设者的产出就不再能说明任何问题了。
        """
        tmp = _fake_home([REAL_LINE])
        try:
            text = environ.block(Path(tmp.name))
        finally:
            tmp.cleanup()
        for leak in ("game.js", "three", "bloom", "tier", "里程", "星空",
                     "假设", "应该改", "建议改"):
            self.assertNotIn(leak, text.lower() if leak.isascii() else text)


class TestVerifierPrompt(unittest.TestCase):
    def test_env_block_and_user_hint_are_separate_sections(self):
        """人写的提示和自动探测的环境必须分开标注，否则分不清哪句是谁说的。"""
        from hypoloop.roles import verifier

        p = verifier.build_prompt(
            "随便什么任务", Path("."), {"evidence_hint": "我自己叮嘱的一句话"},
            {"hypotheses": []}, {"critiques": []}, [])
        self.assertIn("我自己叮嘱的一句话", p)
        self.assertIn("【调用方给的取证提示】", p)

    def test_no_hint_section_when_user_gave_none(self):
        # 默认就该是空的 —— 人不写，提示词里就不该冒出一个空的提示小节
        from hypoloop.roles import verifier

        p = verifier.build_prompt(
            "随便什么任务", Path("."), {}, {"hypotheses": []}, {"critiques": []}, [])
        self.assertNotIn("【调用方给的取证提示】", p)


if __name__ == "__main__":
    unittest.main()
