"""订正必须被逐条交代 —— 防的是「接受裁决、忽略订正」这一类。

真实事故：质疑者正确指出假设者把跨双轨所需位移算成了碰撞体全宽 3.70m（几何错了，
实际 4.35m），验证者采纳了 `revise` 这个**裁决**，却把 3.70 原样写进代码，留下
51.0~61.5 m/s 段必死率 9% 的窟窿，下一轮假设者才把它抓出来。

根因不是模型笨，是 schema 设计：裁决（keep/revise/reject）是结构化字段，具体的
数值订正只在 flaw / revision 的自由文本里 —— 于是可以被静默跳过。

三层修复，这里全都盯着：
  1. schema  —— corrections 提成一等字段，且每条 critique 必填
  2. 提示词  —— 订正摊平成编号清单摆到验证者面前，明说「采纳裁决 ≠ 采纳数值」
  3. 机械核对 —— 跑完比对 id 覆盖，漏了就点名
"""

import io
import json
import unittest
from pathlib import Path

from hypoloop import config
from hypoloop.loop import _correction_report
from hypoloop.roles import verifier

# 照着真实那一轮构造
REAL_CRITIQUE = {
    "critiques": [
        {
            "hypothesis_id": "h1-outer-swap-negative-margin",
            "flaw": "坐标几何计算错误：假设者以碰撞体全宽 2*1.85 = 3.70 m 作为位移量",
            "severity": "medium",
            "verdict": "revise",
            "revision": "按实际几何重算所需位移",
            "corrections": [{
                "id": "c1",
                "what": "跨越双轨（0→2）所需的横向位移",
                "wrong": "3.70 m",
                "correct": "4.35 m",
                "basis": "车道 x=-2.5/0/2.5，碰撞条件 |dx|<1.85；"
                         "从 -2.5 走到 +1.85 即 4.35 m",
            }],
        },
        {
            "hypothesis_id": "h2-jump-lockout",
            "flaw": "freeLane 永远留空，全通道阻断不可能发生",
            "severity": "high",
            "verdict": "reject",
            "corrections": [],
        },
    ],
    "counter_hypotheses": [],
    "priority_order": [],
}


class TestSchemas(unittest.TestCase):
    def _schema(self, name):
        with io.open(config.schema(name), encoding="utf-8") as fh:
            return json.load(fh)

    def test_corrections_is_required_on_every_critique(self):
        item = self._schema("critique")["properties"]["critiques"]["items"]
        self.assertIn("corrections", item["required"])
        fix = item["properties"]["corrections"]["items"]
        # 五个字段一个都不能少 —— 少了 correct 或 basis，订正就退化成又一段自由文本
        for f in ("id", "what", "wrong", "correct", "basis"):
            self.assertIn(f, fix["required"], f)

    def test_verification_requires_addressing_corrections(self):
        s = self._schema("verification")
        self.assertIn("corrections_addressed", s["required"])
        it = s["properties"]["corrections_addressed"]["items"]
        self.assertEqual(set(it["properties"]["action"]["enum"]),
                         {"applied", "rejected", "not_applicable"})
        self.assertIn("evidence", it["required"])


class TestFlatten(unittest.TestCase):
    def test_collects_corrections_across_critiques(self):
        got = verifier.corrections(REAL_CRITIQUE)
        self.assertEqual([c["id"] for c in got], ["c1"])
        self.assertEqual(got[0]["correct"], "4.35 m")
        # 要能追回是哪条假设的订正
        self.assertEqual(got[0]["hypothesis_id"], "h1-outer-swap-negative-margin")

    def test_survives_junk(self):
        for junk in ({}, {"critiques": None}, {"critiques": [{}]},
                     {"critiques": [{"corrections": [{"no_id": 1}]}]},
                     {"critiques": [{"corrections": "不是数组"}]}):
            self.assertEqual(verifier.corrections(junk), [])


class TestChecklistInPrompt(unittest.TestCase):
    def test_checklist_shows_both_values_and_the_basis(self):
        text = verifier.corrections_checklist(REAL_CRITIQUE)
        for token in ("c1", "3.70", "4.35", "1.85", "一条不许漏"):
            self.assertIn(token, text)

    def test_checklist_says_verdict_is_not_the_value(self):
        # 这句话是整个修复的要点，别在重构里丢了
        text = verifier.corrections_checklist(REAL_CRITIQUE)
        self.assertIn("不等于采纳了订正的数值", text)

    def test_empty_case_is_explicit_not_blank(self):
        text = verifier.corrections_checklist({"critiques": []})
        self.assertIn("没给出", text)

    def test_prompt_embeds_the_checklist(self):
        p = verifier.build_prompt("任务", Path("."), {},
                                  {"hypotheses": []}, REAL_CRITIQUE, [])
        self.assertIn("订正清单", p)
        self.assertIn("4.35 m", p)


class TestMechanicalCheck(unittest.TestCase):
    def test_missed_correction_is_called_out_with_the_numbers(self):
        verify = {"corrections_addressed": []}
        lines = "\n".join(_correction_report(REAL_CRITIQUE, verify))
        self.assertIn("没被交代", lines)
        self.assertIn("c1", lines)
        self.assertIn("4.35 m", lines)
        self.assertIn("3.70 m", lines)

    def test_applied_correction_is_quiet(self):
        verify = {"corrections_addressed": [
            {"correction_id": "c1", "action": "applied",
             "evidence": "dx_min_swap 改为 4.35，全速度段必死率 0/200000"}]}
        lines = "\n".join(_correction_report(REAL_CRITIQUE, verify))
        self.assertIn("交代了 1 条", lines)
        self.assertNotIn("没被交代", lines)

    def test_rejected_correction_is_surfaced_for_review(self):
        # 反驳订正是允许的，但必须让人看见，不能悄悄过去
        verify = {"corrections_addressed": [
            {"correction_id": "c1", "action": "rejected",
             "evidence": "实测 4.35 会导致 30% 波次退化为单一车道"}]}
        lines = "\n".join(_correction_report(REAL_CRITIQUE, verify))
        self.assertIn("反驳了这条订正", lines)
        self.assertIn("30% 波次", lines)

    def test_no_corrections_means_no_noise(self):
        self.assertEqual(_correction_report({"critiques": []}, {}), [])

    def test_unknown_id_does_not_count_as_coverage(self):
        # 交代了一条不存在的 id，不能拿来充抵真正漏掉的那条
        verify = {"corrections_addressed": [
            {"correction_id": "c99", "action": "applied", "evidence": "无关"}]}
        lines = "\n".join(_correction_report(REAL_CRITIQUE, verify))
        self.assertIn("没被交代", lines)
        self.assertIn("交代了 0 条", lines)


if __name__ == "__main__":
    unittest.main()
