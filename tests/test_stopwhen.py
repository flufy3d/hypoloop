"""`--until` 早停的测试。

动机：`--rounds 5` 但第 2 轮就收敛了，后面三轮每轮还要烧一百多万 token。而且没东西
可改时硬凑三条假设，会逼着它去动不该动的地方 —— 浪费之外还有风险。

**这个功能的失败方向必须是「多跑一轮」，绝不能是「错误地提前停」。**
多跑一轮只是费点 token；错误收工会把没验完的改动留在别人的仓库里。
所以下面绝大多数用例检查的都是「什么情况下**不**停」。
"""

import io
import json
import unittest
from pathlib import Path

from hypoloop import config
from hypoloop.loop import _goal_met
from hypoloop.roles import verifier

CFG = {"stop_when": "全速度段必死率为 0 且难度常数未改动"}
GOOD = {"verify": {"goal": {"met": True, "evidence": "必死 0/200000；gap/速度上限未改"}}}


def quiet(_msg):
    pass


class TestStopsOnlyOnRealEvidence(unittest.TestCase):
    def test_stops_when_met_with_evidence(self):
        self.assertTrue(_goal_met(CFG, GOOD, quiet))

    def test_never_stops_without_until(self):
        for cfg in ({}, {"stop_when": ""}, {"stop_when": "   "}, {"stop_when": None}):
            self.assertFalse(_goal_met(cfg, GOOD, quiet), cfg)

    def test_does_not_stop_when_goal_missing(self):
        # goal 在 schema 里是可选的 —— 缺失就是没判过，不能当成达成
        self.assertFalse(_goal_met(CFG, {"verify": {}}, quiet))
        self.assertFalse(_goal_met(CFG, {}, quiet))

    def test_does_not_stop_on_falsy_or_fuzzy_met(self):
        for met in (False, None, "true", "yes", 1, "", 0):
            rnd = {"verify": {"goal": {"met": met, "evidence": "有证据"}}}
            self.assertFalse(_goal_met(CFG, rnd, quiet), repr(met))

    def test_does_not_stop_without_evidence(self):
        for ev in ("", "   ", None):
            rnd = {"verify": {"goal": {"met": True, "evidence": ev}}}
            self.assertFalse(_goal_met(CFG, rnd, quiet), repr(ev))

    def test_says_out_loud_why_it_refused(self):
        said = []
        rnd = {"verify": {"goal": {"met": True, "evidence": ""}}}
        _goal_met(CFG, rnd, said.append)
        self.assertTrue(any("没给证据" in s for s in said), said)

    def test_does_not_stop_when_the_round_errored(self):
        # 那一轮本身就崩了，它的结论不可信，更不能拿来收工
        rnd = dict(GOOD, error="工作区指纹变了")
        self.assertFalse(_goal_met(CFG, rnd, quiet))

    def test_junk_goal_shapes_do_not_crash_or_stop(self):
        for goal in ("达成了", ["met"], 1, None, {"met": True}):
            rnd = {"verify": {"goal": goal}}
            self.assertFalse(_goal_met(CFG, rnd, quiet), repr(goal))

    def test_prints_the_evidence_for_the_human(self):
        # 收工要当着人的面把证据摊开，让人能当场质疑
        said = []
        _goal_met(CFG, GOOD, said.append)
        self.assertTrue(any("0/200000" in s for s in said), said)


class TestSchema(unittest.TestCase):
    def test_goal_is_optional_so_omission_never_stops_early(self):
        with io.open(config.schema("verification"), encoding="utf-8") as fh:
            s = json.load(fh)
        self.assertIn("goal", s["properties"])
        self.assertNotIn("goal", s["required"])
        self.assertEqual(set(s["properties"]["goal"]["required"]),
                         {"met", "evidence"})


class TestPrompt(unittest.TestCase):
    def test_prompt_is_silent_when_no_condition_set(self):
        p = verifier.build_prompt("任务", Path("."), {},
                                  {"hypotheses": []}, {"critiques": []}, [])
        self.assertNotIn("【结束条件】", p)

    def test_prompt_states_the_condition_and_the_asymmetry(self):
        p = verifier.build_prompt("任务", Path("."), CFG,
                                  {"hypotheses": []}, {"critiques": []}, [])
        self.assertIn("【结束条件】", p)
        self.assertIn("全速度段必死率为 0", p)
        # 关键：必须告诉它两种错误的代价不对等，否则它会倾向于宣布完成
        self.assertIn("一律 false", p)
        self.assertIn("别为了让任务显得完成", p)


class TestLoopWiring(unittest.TestCase):
    def test_break_happens_after_the_round_is_recorded(self):
        """先把这一轮记进 rounds 再判停 —— 否则报告里会缺最后一轮。"""
        import inspect

        from hypoloop import loop

        src = inspect.getsource(loop.run)
        self.assertLess(src.index("rounds.append(_one_round("),
                        src.index("_goal_met"))

    def test_dry_run_never_stops_early(self):
        import inspect

        from hypoloop import loop

        self.assertIn("not dry_run and _goal_met", inspect.getsource(loop.run))


if __name__ == "__main__":
    unittest.main()
