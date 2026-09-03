"""超时抢救的测试。

agy 的 print 模式到点就把 agent 已完成的工作全部丢弃，只回一个 status=ERROR。
实测被这个坑掉过一次：45 分钟、12.6 万 token、36 次输出，最后拿到一个空壳。
salvage 用 --conversation 接回那段会话，只把结论要回来。
"""

import unittest
from pathlib import Path

from hypoloop import agy


class TestSalvageArgv(unittest.TestCase):
    def test_conversation_id_becomes_a_flag(self):
        argv = agy.build_argv("agy", model="m", mode="accept-edits",
                              conversation_id="abc-123")
        self.assertEqual(argv[argv.index("--conversation") + 1], "abc-123")

    def test_no_conversation_flag_when_absent(self):
        self.assertNotIn("--conversation",
                         agy.build_argv("agy", model="m", mode="plan"))


class _Recorder:
    """记下 run_agy 被怎么调的，并按脚本返回。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append((prompt, kw))
        return self.result


class TestSalvage(unittest.TestCase):
    def setUp(self):
        self._orig = agy.run_agy

    def tearDown(self):
        agy.run_agy = self._orig

    def _salvage(self, failed, rescued):
        agy.run_agy = _Recorder(rescued)
        out = agy.salvage(failed, cwd=Path("."), model="m", mode="accept-edits")
        return out, agy.run_agy.calls

    def test_returns_none_without_a_conversation_id(self):
        # 没有会话 id 就无从续接 —— 别去发一个注定失败的请求
        agy.run_agy = _Recorder(agy.AgyCall(status="SUCCESS"))
        failed = agy.AgyCall(status="ERROR", error="timeout waiting for response")
        self.assertIsNone(
            agy.salvage(failed, cwd=Path("."), model="m", mode="accept-edits"))
        self.assertEqual(agy.run_agy.calls, [])

    def test_resumes_the_same_conversation(self):
        failed = agy.AgyCall(status="ERROR", conversation_id="conv-9")
        good = agy.AgyCall(status="SUCCESS",
                           structured_output={"summary": "ok", "evidence": []})
        out, calls = self._salvage(failed, good)
        self.assertIsNotNone(out)
        self.assertEqual(calls[0][1]["conversation_id"], "conv-9")
        self.assertEqual(out["salvaged_from"], "conv-9")

    def test_salvage_prompt_forbids_new_work_and_invented_readings(self):
        failed = agy.AgyCall(status="ERROR", conversation_id="conv-9")
        good = agy.AgyCall(status="SUCCESS", structured_output={"summary": "ok"})
        _, calls = self._salvage(failed, good)
        prompt = calls[0][0]
        self.assertIn("不要再做任何新工作", prompt)
        self.assertIn("inconclusive", prompt)
        self.assertIn("不许编造", prompt)

    def test_failed_salvage_returns_none_not_a_half_result(self):
        # 抢救失败就该老实返回 None，让调用方按原样报错；
        # 回一个没有 structured_output 的壳会让上层以为这一步成了
        failed = agy.AgyCall(status="ERROR", conversation_id="conv-9")
        for bad in (agy.AgyCall(status="ERROR", conversation_id="conv-9"),
                    agy.AgyCall(status="SUCCESS")):          # 成功但没产出
            out, _ = self._salvage(failed, bad)
            self.assertIsNone(out)

    def test_salvage_uses_a_short_timeout(self):
        failed = agy.AgyCall(status="ERROR", conversation_id="conv-9")
        good = agy.AgyCall(status="SUCCESS", structured_output={"summary": "ok"})
        agy.run_agy = _Recorder(good)
        agy.salvage(failed, cwd=Path("."), model="m", mode="accept-edits",
                    timeout_sec=300)
        self.assertEqual(agy.run_agy.calls[0][1]["timeout_sec"], 300)


if __name__ == "__main__":
    unittest.main()
