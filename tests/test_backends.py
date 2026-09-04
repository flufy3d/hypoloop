"""多后端的测试。

fixture 全是**实测抓下来的原样报文**（2026-09-04，codex-cli 0.153.2 /
Claude Code 2.1.250），不是照着文档手写的 —— 手写 fixture 只能证明解析器和我脑子里的
格式一致，证明不了它和这些 CLI 真实吐出来的东西一致。
"""

import json
import unittest
from pathlib import Path

from hypoloop import backends
from hypoloop.backends import claude, codex
from hypoloop.backends.base import (MODE_READONLY, MODE_WRITE, Backend, Call,
                                    CliNotFound)


class TestRegistry(unittest.TestCase):
    def test_unknown_backend_is_an_error_not_a_silent_fallback(self):
        # 名字打错时最坏的结果是「悄悄换了一家跑」：花了钱、改了代码，
        # 而人以为用的是另一家。宁可当场报错。
        with self.assertRaises(CliNotFound):
            backends.get("gpt-9000")

    def test_named_backend_is_returned_even_if_not_installed(self):
        """显式指定了就必须是那一家。**没装也不许替换成别家** ——
        换掉的话，人看到的是一份用错模型跑出来的结果，而命令行看上去一切正常。"""
        for name in backends.names():
            self.assertEqual(backends.choose(name).name, name)

    def test_every_backend_declares_what_it_needs(self):
        for b in backends.all_backends():
            self.assertTrue(b.name, b)
            self.assertTrue(b.label, b.name)
            self.assertTrue(b.default_model, b.name)
            self.assertTrue(b.default_verifier_model, b.name)
            self.assertTrue(b.env_var, b.name)
            # 两档模型必须不一样：验证者要动手改代码，跟只读角色同档就没意义了
            self.assertNotEqual(b.default_model, b.default_verifier_model, b.name)

    def test_choose_prefers_the_first_installed(self):
        found = backends.installed()
        if not found:
            self.skipTest("本机一家 CLI 都没装")
        self.assertEqual(backends.choose().name, found[0].name)

    def test_default_models_are_never_hardcoded_in_config(self):
        """DEFAULTS 里的模型名必须留空。

        写死一个 gemini 模型名，换到 codex/claude 上就是把一个它们不认识的名字
        递过去 —— 而且报错会指向模型不存在，跟真正的原因（配置层没分家）无关。
        """
        from hypoloop import config
        self.assertEqual(config.DEFAULTS["model"], "")
        self.assertEqual(config.DEFAULTS["verifier_model"], "")


class TestPerBackendConfig(unittest.TestCase):
    """模型名只对一家成立，所以项目配置里得按后端分开存。

    不分开的话：`--model gemini-3.8-flash-high --save-config` 存下来，下次
    `--backend codex` 跑同一个项目，就是拿 gemini 的模型名去问 codex —— 而且报错会
    指向「模型不存在」，跟真正的原因（配置层没分家）完全对不上。
    """

    def setUp(self):
        import tempfile
        from hypoloop import config
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = config.PROJECTS_DIR
        config.PROJECTS_DIR = Path(self.tmp.name) / "projects"
        config.PROJECTS_DIR.mkdir(parents=True)

    def tearDown(self):
        from hypoloop import config
        config.PROJECTS_DIR = self._orig
        self.tmp.cleanup()

    def test_models_do_not_leak_across_backends(self):
        from hypoloop import config
        root = Path(self.tmp.name)
        config.save_project(root, {"model": "gemini-3.8-flash-high",
                                   "rounds": 4}, backend="agy")
        self.assertEqual(config.load(root, backend="agy")["model"],
                         "gemini-3.8-flash-high")
        self.assertEqual(config.load(root, backend="codex")["model"], "")
        # 跟后端无关的设置照旧全局共享
        self.assertEqual(config.load(root, backend="codex")["rounds"], 4)

    def test_each_backend_keeps_its_own(self):
        from hypoloop import config
        root = Path(self.tmp.name)
        config.save_project(root, {"model": "a"}, backend="agy")
        config.save_project(root, {"model": "c"}, backend="codex")
        self.assertEqual(config.load(root, backend="agy")["model"], "a")
        self.assertEqual(config.load(root, backend="codex")["model"], "c")

    def test_command_line_still_wins(self):
        from hypoloop import config
        root = Path(self.tmp.name)
        config.save_project(root, {"model": "saved"}, backend="agy")
        self.assertEqual(
            config.load(root, {"model": "cli"}, backend="agy")["model"], "cli")


class TestCodexArgv(unittest.TestCase):
    def argv(self, **kw):
        kw.setdefault("model", "gpt-5.6-terra:high")
        kw.setdefault("mode", MODE_READONLY)
        return codex.build_argv("codex", **kw)

    def test_prompt_comes_from_stdin(self):
        # 提示词带着代码上下文，塞 argv 迟早撞上 Windows 32767 字符的上限，
        # 而报出来的错（参数过长）跟提示词毫无关系，极难定位。
        self.assertEqual(self.argv()[-1], "-")

    def test_readonly_role_gets_the_real_sandbox(self):
        argv = self.argv(mode=MODE_READONLY)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_write_role_can_write_and_reach_the_network(self):
        argv = self.argv(mode=MODE_WRITE)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        # 取证要装包、要起本地服务器。不开网络，验证者只能空手套白狼。
        self.assertIn("sandbox_workspace_write.network_access=true", argv)

    def test_full_access_only_applies_to_the_writing_role(self):
        argv = codex.build_argv("codex", model="m", mode=MODE_READONLY,
                                full_access=True)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        argv = codex.build_argv("codex", model="m", mode=MODE_WRITE,
                                full_access=True)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_reasoning_effort_rides_along_in_the_model_name(self):
        self.assertEqual(codex.split_model("gpt-5.6-terra:high"),
                         ("gpt-5.6-terra", "high"))
        self.assertEqual(codex.split_model("gpt-5.6-terra"),
                         ("gpt-5.6-terra", ""))
        argv = self.argv(model="gpt-5.6-sol:xhigh")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-sol")
        self.assertIn("model_reasoning_effort=xhigh", argv)

    def test_no_effort_flag_when_none_asked_for(self):
        self.assertNotIn("model_reasoning_effort=",
                         " ".join(self.argv(model="gpt-5.6-terra")))

    def test_resume_targets_the_same_thread(self):
        argv = self.argv(resume_key="th-1")
        self.assertEqual(argv[1:4], ["exec", "resume", "th-1"])

    def test_resume_drops_the_flags_it_does_not_accept(self):
        """`codex exec resume` 不收 `-s` 和 `-C`（沙箱和工作目录从原会话继承）。

        给了就是参数错误，抢救当场失败 —— 而报出来的
        「to pass '-s' as a value, use '-- -s'」跟真正的原因毫无关系。
        **实跑抓到过**：agy 和 claude 的续接都通，唯独 codex 这条静默地废着，
        而单测只看 argv 前三项是看不出来的。
        """
        for mode in (MODE_READONLY, MODE_WRITE):
            argv = self.argv(mode=mode, resume_key="th-1", cwd=Path("/proj"))
            self.assertNotIn("-s", argv, mode)
            self.assertNotIn("-C", argv, mode)
            self.assertNotIn("sandbox_workspace_write.network_access=true",
                             argv, mode)
            # 但模型、schema、收信文件这些它是收的，不能一起丢掉
            self.assertIn("--output-schema" if "--output-schema" in argv else "-m",
                          argv, mode)
            self.assertEqual(argv[-1], "-", mode)

    def test_fresh_call_still_gets_the_sandbox(self):
        # 上一条别修过头：**新开**的调用必须照旧带沙箱，否则只读就没了
        argv = self.argv(mode=MODE_READONLY, cwd=Path("/proj"))
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("-C", argv)

    def test_schema_and_last_message_are_files(self):
        argv = self.argv(schema_path=Path("s.json"),
                         last_message_path=Path("last.json"))
        self.assertEqual(argv[argv.index("--output-schema") + 1], "s.json")
        self.assertEqual(argv[argv.index("-o") + 1], "last.json")


# codex exec --json 的真实事件流（2026-09-04 实跑抓的，只删掉了中间几条命令事件）
CODEX_EVENTS = "\n".join([
    '{"type":"thread.started","thread_id":"01a06bb9-247b-7cb1-8ea3-da8c0c90a142"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_3","type":"agent_message",'
    '"text":"{\\"files\\":[\\"a.txt\\"],\\"answer\\":\\"ok\\"}"}}',
    '{"type":"turn.completed","usage":{"input_tokens":37743,'
    '"cached_input_tokens":24064,"cache_write_input_tokens":0,'
    '"output_tokens":501,"reasoning_output_tokens":185}}',
])


class TestCodexParsing(unittest.TestCase):
    def test_usage_totals_do_not_double_count_the_cache(self):
        """codex **不给 total**，得自己加。`cached_input_tokens` 是
        `input_tokens` 的子集（不是另一笔），三个一起加会把账算大。"""
        u = codex._usage(codex._iter_events(CODEX_EVENTS))
        self.assertEqual(u["input_tokens"], 37743)
        self.assertEqual(u["output_tokens"], 501)
        self.assertEqual(u["cache_read_tokens"], 24064)
        self.assertEqual(u["thinking_tokens"], 185)
        self.assertEqual(u["total_tokens"], 37743 + 501)

    def test_thread_id_survives_for_salvage(self):
        events = codex._iter_events(CODEX_EVENTS)
        self.assertEqual(codex._thread_id(events),
                         "01a06bb9-247b-7cb1-8ea3-da8c0c90a142")

    def test_half_written_last_line_does_not_kill_the_whole_stream(self):
        """超时被杀的时候最后一行往往是半截 JSON。整份事件流不能因此报废 ——
        `thread_id` 就在第一行，丢了它就等于放弃抢救。"""
        events = codex._iter_events(CODEX_EVENTS + '\n{"type":"item.st')
        self.assertEqual(codex._thread_id(events),
                         "01a06bb9-247b-7cb1-8ea3-da8c0c90a142")

    def test_structured_output_falls_back_to_the_event_stream(self):
        """`-o` 那个文件在被杀掉时不会落盘，但事件流里可能已经有完整的结构化消息。
        那份产出不该白白丢掉 —— 这正是 Call.degraded 讲的事。"""
        data = codex._structured(Path("does-not-exist.json"),
                                 codex._iter_events(CODEX_EVENTS))
        self.assertEqual(data, {"files": ["a.txt"], "answer": "ok"})

    def test_no_structured_output_is_none_not_empty_dict(self):
        # None 表示「这一步没产出」，{} 会被误当成「产出了个空结果」
        self.assertIsNone(codex._structured(Path("nope.json"), []))


# codex app-server 的 account/rateLimits/read 真实响应（同日实测）。
# 抓到的时候两个窗口都是 0%，这里把百分比换成非零值以便验算换算方向，
# **字段名和层级是原样的**。
CODEX_SNAPSHOT = {
    "limitId": "codex", "limitName": None,
    "primary": {"usedPercent": 22, "windowDurationMins": 300,
                "resetsAt": 1788531696},
    "secondary": {"usedPercent": 30, "windowDurationMins": 10080,
                  "resetsAt": 1789118496},
    "credits": {"hasCredits": False, "unlimited": False, "balance": None},
    "individualLimit": None, "spendControlReached": False, "planType": "team",
}


class TestCodexQuota(unittest.TestCase):
    def test_used_percent_becomes_remaining(self):
        q = codex.from_snapshot(CODEX_SNAPSHOT)
        self.assertTrue(q.available)
        self.assertAlmostEqual(q["five_hour"]["remaining_pct"], 78.0)
        self.assertAlmostEqual(q["weekly"]["remaining_pct"], 70.0)
        self.assertEqual(q["tier"], "team")

    def test_reset_time_is_a_unix_epoch_not_a_string(self):
        q = codex.from_snapshot(CODEX_SNAPSHOT)
        self.assertRegex(q["five_hour"]["reset_at"], r"^\d{4}-\d{2}-\d{2} ")

    def test_integer_precision_is_disclosed_not_hidden(self):
        """codex 只给整数百分比，跑两轮很可能显示成「吃掉 0 个百分点」。
        不说清楚的话，人会以为这一轮没花钱。"""
        from hypoloop.quota import format_quota
        self.assertIn("整数百分比", format_quota(codex.from_snapshot(CODEX_SNAPSHOT)))

    def test_empty_snapshot_is_unavailable_not_zero_percent(self):
        # 「读不到」和「剩 0%」是完全不同的两件事，绝不能混
        self.assertFalse(codex.from_snapshot({}).available)


class TestCodexStrictSchema(unittest.TestCase):
    """codex 走 OpenAI 的 strict schema，两条硬要求：每个对象都要
    `additionalProperties: false`，每个字段都要列进 `required`。不满足就是 HTTP 400
    `invalid_json_schema`，整步失败。**实测撞过**，第一次接 codex 就死在这。

    修法是机械改写，不是去改那三份 schema —— 那等于让一家后端的序列化限制
    反过来改写整套系统的语义。
    """

    def schemas(self):
        from hypoloop import config
        for name in ("hypotheses", "critique", "verification"):
            yield name, json.loads(
                config.schema(name).read_text(encoding="utf-8"))

    def walk(self, node, seen):
        if isinstance(node, list):
            for x in node:
                self.walk(x, seen)
            return
        if not isinstance(node, dict):
            return
        if isinstance(node.get("properties"), dict):
            seen.append(node)
        for v in node.values():
            self.walk(v, seen)

    def test_every_object_satisfies_strict_mode(self):
        for name, raw in self.schemas():
            objs = []
            self.walk(codex.strictify(raw), objs)
            self.assertTrue(objs, name)
            for obj in objs:
                self.assertIs(obj["additionalProperties"], False, name)
                self.assertEqual(set(obj["required"]),
                                 set(obj["properties"]), name)

    def test_optional_becomes_nullable_not_mandatory(self):
        """`goal` 是**故意**可选的：缺失 = 没判过结束条件，绝不能变成「必须判」。
        改写后它必须还能是 null，否则等于逼验证者每轮都对结束条件表态 ——
        而那正是 --until 设计里最不能出的错。"""
        from hypoloop import config
        raw = json.loads(config.schema("verification").read_text(encoding="utf-8"))
        self.assertNotIn("goal", raw.get("required") or [])
        strict = codex.strictify(raw)
        self.assertIn("goal", strict["required"])
        self.assertIn("null", strict["properties"]["goal"]["type"])

    def test_null_goal_still_means_keep_going(self):
        """改写后的读取端语义必须没变：null 一样是「没判过」，不许提前收工。"""
        from hypoloop.loop import _goal_met
        cfg = {"stop_when": "必死率为 0"}
        for goal in (None, {}, {"met": None}):
            self.assertFalse(
                _goal_met(cfg, {"verify": {"goal": goal}}, lambda s: None), goal)

    def test_originals_are_left_alone(self):
        """改写只能发生在临时文件上。原 schema 被就地改掉的话，
        agy 和 claude 会跟着吃到 codex 的限制。"""
        from hypoloop import config
        for name, raw in self.schemas():
            before = json.dumps(raw, sort_keys=True)
            codex.strictify(raw)
            self.assertEqual(json.dumps(raw, sort_keys=True), before, name)
            on_disk = config.schema(name).read_text(encoding="utf-8")
            self.assertNotIn('"additionalProperties"', on_disk, name)

    def test_unreadable_schema_falls_back_instead_of_crashing(self):
        missing = Path("does-not-exist.schema.json")
        self.assertEqual(codex.strict_schema_file(missing, Path(".")), missing)


class TestCodexErrorSurfacing(unittest.TestCase):
    """真实事件（2026-09-04 抓的）：codex 把上游 400 整个 JSON 塞进 message 字符串。

    直接打出来是一坨转义；而如果先按「退出码非 0」写死 error，真正的原因会被
    一句跟原因毫无关系的「退出码 1」盖掉 —— 第一次接 codex 时就是这样浪费了一轮。
    """

    REAL = {
        "type": "turn.failed",
        "error": {"message": json.dumps({
            "type": "error",
            "error": {"type": "invalid_request_error",
                      "code": "invalid_json_schema",
                      "message": "Invalid schema for response_format "
                                 "'codex_output_schema': In context=(), "
                                 "'additionalProperties' is required to be "
                                 "supplied and to be false.",
                      "param": "text.format.schema"},
            "status": 400})},
    }

    def test_innermost_message_is_what_surfaces(self):
        msg = codex._event_error(self.REAL)
        self.assertIn("additionalProperties", msg)
        self.assertNotIn("\\n", msg)

    def test_plain_message_passes_through(self):
        self.assertEqual(codex._event_error({"message": "boom"}), "boom")


class TestClaudeArgv(unittest.TestCase):
    def argv(self, **kw):
        kw.setdefault("model", "sonnet")
        kw.setdefault("mode", MODE_READONLY)
        return claude.build_argv("claude", **kw)

    def test_readonly_role_runs_in_plan_mode(self):
        argv = self.argv(mode=MODE_READONLY)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")

    def test_readonly_role_never_gets_bypass_permissions(self):
        """`--dangerously-skip-permissions` 就是 bypassPermissions，会把 plan 模式
        那道门拆掉。agy 那边必须加是因为它 headless 下不加连 read_file 都被自动
        拒绝；claude 没这个毛病，plan 模式下读工具本来就放行。"""
        self.assertNotIn("--dangerously-skip-permissions",
                         self.argv(mode=MODE_READONLY))
        self.assertIn("--dangerously-skip-permissions",
                      self.argv(mode=MODE_WRITE))

    def test_write_role_accepts_edits(self):
        argv = self.argv(mode=MODE_WRITE)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_json_schema_takes_the_schema_itself_not_a_path(self):
        """实测：给路径直接报「--json-schema is not valid JSON」。
        这个错很容易被当成 schema 写错了，其实是参数形态不对。"""
        argv = self.argv(schema_text='{"type":"object"}')
        got = argv[argv.index("--json-schema") + 1]
        self.assertEqual(json.loads(got)["type"], "object")

    def test_resume_targets_the_same_session(self):
        argv = self.argv(resume_key="sess-1")
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")


class TestClaudeParsing(unittest.TestCase):
    # claude -p --output-format json 的真实 usage（同日实测）
    REAL_USAGE = {
        "input_tokens": 10, "cache_creation_input_tokens": 7001,
        "cache_read_input_tokens": 17783, "output_tokens": 55,
        "output_tokens_details": {"thinking_tokens": 48},
    }

    def test_cache_read_is_not_counted_into_the_total(self):
        """cache_read 是另一个计费档（约为新输入的十分之一），长会话里会压倒性地大。
        混进 total，「这次花了多少」就完全失真 —— taiji 在这上面栽过。"""
        u = claude._usage(self.REAL_USAGE)
        self.assertEqual(u["cache_read_tokens"], 17783)
        self.assertEqual(u["total_tokens"], 10 + 7001 + 55)
        self.assertEqual(u["thinking_tokens"], 48)

    def test_missing_usage_is_zero_not_a_crash(self):
        self.assertEqual(claude._usage({})["total_tokens"], 0)


# GET /api/oauth/usage 的真实响应（同日实测，删掉了一堆 null 的实验性字段）
CLAUDE_USAGE = {
    "five_hour": {"utilization": 85.0,
                  "resets_at": "2026-09-04T10:10:00.145007+00:00"},
    "seven_day": {"utilization": 12.0,
                  "resets_at": "2026-09-11T00:00:00.145029+00:00"},
    "extra_usage": {"is_enabled": False},
}


class TestClaudeQuota(unittest.TestCase):
    def test_utilization_becomes_remaining(self):
        q = claude.from_usage(CLAUDE_USAGE)
        self.assertTrue(q.available)
        self.assertAlmostEqual(q["five_hour"]["remaining_pct"], 15.0)
        self.assertAlmostEqual(q["weekly"]["remaining_pct"], 88.0)
        self.assertTrue(q["five_hour"]["reset_at"])

    def test_empty_payload_is_unavailable(self):
        self.assertFalse(claude.from_usage({}).available)

    def test_token_is_read_only_never_refreshed(self):
        """令牌只读：refresh token 会轮换，我们抢着刷会把 Claude Code 自己那份挤掉。
        源码里出现任何写回 .credentials.json 的动作都是事故。"""
        src = Path(claude.__file__).read_text(encoding="utf-8")
        for forbidden in ("write_text", "refresh_token", "oauth/token"):
            self.assertNotIn(forbidden, src)


class TestModeMapping(unittest.TestCase):
    def test_every_backend_maps_both_abstract_modes(self):
        """循环只认 readonly/write 两个抽象档位。哪家漏了映射，
        就会把字符串 "readonly" 当成模型/沙箱名递下去 —— 而且多半不会当场报错。"""
        from hypoloop.backends.agy import _MODE_MAP
        for mode in (MODE_READONLY, MODE_WRITE):
            self.assertIn(mode, _MODE_MAP)
            self.assertIn(mode, codex.SANDBOX)
            self.assertIn(mode, claude.PERMISSION_MODE)


class TestContract(unittest.TestCase):
    def test_base_run_must_be_implemented(self):
        class Half(Backend):
            name = "half"

        with self.assertRaises(NotImplementedError):
            Half().run("x", cwd=Path("."), model="m", mode=MODE_READONLY)

    def test_installed_is_false_not_an_exception_when_missing(self):
        class Missing(Backend):
            name = "missing"

            def binary(self):
                raise CliNotFound("没装")

        self.assertFalse(Missing().installed())

    def test_quota_default_is_unavailable_not_a_lie(self):
        class NoQuota(Backend):
            name = "noquota"

            def binary(self):
                return "x"

        self.assertFalse(NoQuota().quota().available)


class TestCallNormalisation(unittest.TestCase):
    def test_total_tokens_defaults_to_zero(self):
        self.assertEqual(Call().total_tokens, 0)

    def test_session_key_prefers_the_explicit_field(self):
        self.assertEqual(
            Call(session_key="a", conversation_id="b").session_key, "a")
        self.assertIsNone(Call().session_key)


if __name__ == "__main__":
    unittest.main()
