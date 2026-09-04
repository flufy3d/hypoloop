"""额度解析的测试。

fixture 是从真实的 agy 1.1.25 日志里截下来的，不是手写的 —— 手写 fixture 只能证明
解析器和我脑子里的格式一致，证明不了它和 agy 真实吐出来的东西一致。

解析代码在 hypoloop/backends/agy.py：额度探针是**一家一个**，没有统一抽象，
因为三家的口子完全不同（日志 / JSON-RPC / HTTP）。统一的只有读数类型（quota.py）。
"""

import unittest
from pathlib import Path

from hypoloop import quota as shared
from hypoloop.backends import agy as quota

FIXTURE = Path(__file__).parent / "fixtures" / "agy-quota.log"


class TestLogParsing(unittest.TestCase):
    def setUp(self):
        self.text = FIXTURE.read_text(encoding="utf-8")
        self.bodies = quota.log_bodies(self.text)

    def test_extracts_quota_body(self):
        self.assertIn(quota.QUOTA_MARK, self.bodies)
        self.assertIn("groups", self.bodies[quota.QUOTA_MARK])

    def test_extracts_tier_and_prefers_paid(self):
        # loadCodeAssist 同时给 currentTier(free-tier) 和 paidTier —— 报付费那个
        self.assertIn("g1-pro-tier", quota._tier(self.bodies[quota.TIER_MARK]))

    def test_picks_gemini_group_not_the_other_one(self):
        groups = self.bodies[quota.QUOTA_MARK]["groups"]
        self.assertGreater(len(groups), 1, "fixture 里应该有不止一个模型组")
        picked = quota.pick_group(groups, "gemini-3.8-flash-medium")
        self.assertEqual(picked["displayName"], "Gemini Models")
        other = quota.pick_group(groups, "claude-sonnet-4-6")
        self.assertEqual(other["displayName"], "Claude and GPT models")

    def test_unknown_model_falls_back_to_first_group(self):
        groups = self.bodies[quota.QUOTA_MARK]["groups"]
        self.assertEqual(quota.pick_group(groups, "wat-9000"), groups[0])
        self.assertIsNone(quota.pick_group([], "gemini"))

    def test_buckets_convert_to_percent(self):
        groups = self.bodies[quota.QUOTA_MARK]["groups"]
        buckets = {b["window"]: b
                   for b in quota.pick_group(groups, "gemini")["buckets"]}
        five = quota._bucket(buckets, "5h")
        self.assertAlmostEqual(five["remaining_pct"], 99.745, places=2)
        self.assertIsNotNone(five["reset_at"])
        weekly = quota._bucket(buckets, "weekly")
        self.assertAlmostEqual(weekly["remaining_pct"], 99.958, places=2)

    def test_missing_bucket_is_none_not_zero(self):
        # 「读不到」和「剩 0%」是两回事，混起来会报出灾难性的假警报
        b = quota._bucket({}, "5h")
        self.assertIsNone(b["remaining_pct"])
        self.assertIsNone(b["remaining_fraction"])


class TestBraceScanner(unittest.TestCase):
    def test_ignores_braces_inside_strings(self):
        self.assertEqual(quota._brace_delta('{"a": "}}}"}'), 0)
        self.assertEqual(quota._brace_delta('{"a": "\\""}'), 0)
        self.assertEqual(quota._brace_delta("{"), 1)
        self.assertEqual(quota._brace_delta("}"), -1)

    def test_truncated_body_is_skipped_not_fatal(self):
        text = ("x http_helpers.go:1] Response from https://h/v1internal:foo:\n"
                "{\n  \"a\": 1\n")     # 没闭合
        self.assertEqual(quota.log_bodies(text), {})

    def test_later_response_wins(self):
        text = ("a] Response from https://h/v1internal:foo:\n{\"v\": 1}\n"
                "b] Response from https://h/v1internal:foo:\n{\"v\": 2}\n")
        self.assertEqual(quota.log_bodies(text)["v1internal:foo"]["v"], 2)

    def test_response_headers_line_is_not_a_body(self):
        # "Response Headers from" 后面跟的是 HTTP 头，不是 JSON —— 认错就全乱
        text = ("a] Response Headers from https://h/v1internal:foo:\n"
                "Content-Type: application/json\n")
        self.assertEqual(quota.log_bodies(text), {})


class TestProbeResilience(unittest.TestCase):
    def test_no_logs_reports_unavailable_without_raising(self):
        q = quota.probe(home=Path(__file__).parent / "does-not-exist", run=False)
        self.assertFalse(q.available)
        self.assertTrue(q.get("note"))

    def test_format_never_invents_numbers(self):
        # 格式化和「吃掉了多少」是**三家共用**的，所以测的是 quota 模块本身
        q = shared.Quota(available=False, note="没读到")
        self.assertIn("读不到", shared.format_quota(q))
        self.assertNotIn("%", shared.format_quota(q))

    def test_consumed_is_none_when_either_side_missing(self):
        good = shared.Quota(available=True, five_hour={"remaining_pct": 90.0},
                            weekly={"remaining_pct": 99.0})
        bad = shared.Quota(available=False, five_hour={}, weekly={})
        self.assertIsNone(shared.consumed(good, bad)["five_hour"])
        after = shared.Quota(five_hour={"remaining_pct": 88.5},
                             weekly={"remaining_pct": 98.5})
        self.assertAlmostEqual(shared.consumed(good, after)["five_hour"], 1.5)


if __name__ == "__main__":
    unittest.main()
