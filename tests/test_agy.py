"""agy 输出解析 + argv 拼装 + 账本的测试。"""

import json
import unittest
from pathlib import Path

from hypoloop import agy, config
from hypoloop.ledger import Ledger

# 真实的 agy --output-format json 输出（agy 1.1.25 实测抄下来的）
REAL = ('{"conversation_id":"aef4c73f","status":"SUCCESS","response":"OK\\n",'
        '"duration_seconds":4.75,"num_turns":1,'
        '"usage":{"input_tokens":13763,"output_tokens":1,"thinking_tokens":0,'
        '"cache_read_tokens":0,"total_tokens":13764}}')


class TestParseOutput(unittest.TestCase):
    def test_plain_json(self):
        call = agy.parse_output(REAL)
        self.assertTrue(call.ok)
        self.assertEqual(call.total_tokens, 13764)

    def test_ignores_progress_lines_before_json(self):
        # agy 会先打 "Fetching available models..." 之类的进度行
        call = agy.parse_output("Fetching available models...\n" + REAL)
        self.assertTrue(call.ok)
        self.assertEqual(call.get("conversation_id"), "aef4c73f")

    def test_structured_output_surfaces_as_data(self):
        blob = json.loads(REAL)
        blob["structured_output"] = {"hypotheses": [{"id": "h1"}]}
        call = agy.parse_output(json.dumps(blob))
        self.assertEqual(call.data["hypotheses"][0]["id"], "h1")

    def test_missing_structured_output_is_none_not_empty_dict(self):
        # None 表示「这一步没产出」，{} 会被误当成「产出了个空结果」
        self.assertIsNone(agy.parse_output(REAL).data)

    def test_empty_and_garbage(self):
        self.assertIsNone(agy.parse_output(""))
        self.assertIsNone(agy.parse_output("segfault\nnope\n"))

    def test_non_success_status(self):
        call = agy.parse_output(REAL.replace("SUCCESS", "ERROR"))
        self.assertFalse(call.ok)

    def test_total_tokens_defaults_to_zero(self):
        self.assertEqual(agy.AgyCall().total_tokens, 0)


class TestArgv(unittest.TestCase):
    def test_readonly_role_never_gets_skip_permissions(self):
        argv = agy.build_argv("agy", model="m", mode=agy.MODE_READONLY)
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_verifier_gets_edit_rights(self):
        argv = agy.build_argv("agy", model="m", mode=agy.MODE_WRITE,
                              allow_edits=True)
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_json_output_and_schema(self):
        argv = agy.build_argv("agy", model="m", mode="plan",
                              schema_path=Path("s.json"))
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--json-schema") + 1], "s.json")

    def test_slash_commands_disabled(self):
        # 提示词里的 /xxx 是数据，不该被当成 slash command 展开
        self.assertIn("--disable-slash-commands",
                      agy.build_argv("agy", model="m", mode="plan"))

    def test_timeout_formatted_for_go_duration(self):
        argv = agy.build_argv("agy", model="m", mode="plan", timeout_sec=900)
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "900s")


class TestSchemasExist(unittest.TestCase):
    def test_all_three_schemas_are_valid_json(self):
        for name in ("hypotheses", "critique", "verification"):
            path = config.schema(name)
            self.assertTrue(path.exists(), "缺 schema：{0}".format(path))
            blob = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(blob["type"], "object")
            self.assertTrue(blob["required"])

    def test_hypothesis_requires_a_falsifiable_prediction(self):
        # 没有 predicted_observable，「往被实证的方向走」就无从谈起
        blob = json.loads(config.schema("hypotheses").read_text(encoding="utf-8"))
        item = blob["properties"]["hypotheses"]["items"]
        self.assertIn("predicted_observable", item["required"])

    def test_verdicts_are_constrained(self):
        blob = json.loads(config.schema("verification").read_text(encoding="utf-8"))
        item = blob["properties"]["evidence"]["items"]
        self.assertEqual(set(item["properties"]["verdict"]["enum"]),
                         {"supported", "refuted", "inconclusive"})


class TestLedger(unittest.TestCase):
    def test_sums_per_role(self):
        led = Ledger()
        led.add("假设者", 1, {"usage": {"total_tokens": 100, "input_tokens": 90,
                                        "output_tokens": 10}})
        led.add("质疑者", 1, {"usage": {"total_tokens": 50}})
        led.add("假设者", 2, {"usage": {"total_tokens": 30}})
        self.assertEqual(led.total, 180)
        self.assertEqual(led.by_role()["假设者"], {"calls": 2, "total_tokens": 130})
        self.assertIn("180", led.render())

    def test_missing_usage_counts_as_zero_not_crash(self):
        led = Ledger()
        led.add("验证者", 1, {"status": "TIMEOUT"})
        self.assertEqual(led.total, 0)


class TestConfig(unittest.TestCase):
    def test_cli_none_does_not_clobber_defaults(self):
        cfg = config.load(Path("."), {"rounds": None, "model": None})
        self.assertEqual(cfg["rounds"], config.DEFAULTS["rounds"])
        self.assertEqual(cfg["model"], config.DEFAULTS["model"])

    def test_cli_value_wins(self):
        self.assertEqual(config.load(Path("."), {"rounds": 5})["rounds"], 5)

    def test_slug_is_a_safe_dir_name(self):
        s = config.slug('让游戏画面更好/更精致 <一些>')
        for ch in '\\/:*?"<>| ':
            self.assertNotIn(ch, s)
        self.assertTrue(s)


if __name__ == "__main__":
    unittest.main()
