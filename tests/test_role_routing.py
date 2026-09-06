import json
import tempfile
import unittest
from pathlib import Path

from hypoloop import config, loop
from hypoloop.backends import Backend, Call, CliNotFound, MODE_READONLY, MODE_WRITE
from hypoloop.guard import WorktreeChanged
from hypoloop.ledger import Ledger
from hypoloop.quota import make, window, unavailable


class FakeBackend(Backend):
    label = "fake"

    def __init__(self, name, tokens):
        self.name, self.tokens = name, tokens
        self.default_model = name + "-mid"
        self.default_verifier_model = name + "-high"
        self.calls, self.rescues, self.probes = [], [], []
        self.fail = self.missing = self.leak = self.salvage_leak = False
        self.no_quota = False

    def env_notes(self):
        return ["ENV-" + self.name]

    def quota(self, model=None):
        self.probes.append(model)
        if self.no_quota:
            return unavailable("fixture missing")
        return make(source="probe", five_hour=window("five_hour", 100 - len(self.probes)),
                    weekly=window("weekly", 100 - len(self.probes)))

    def run(self, prompt, **kw):
        self.calls.append(dict(kw, prompt=prompt))
        if self.missing:
            raise CliNotFound("fixture missing")
        if self.leak:
            (kw["cwd"] / "leak.txt").write_text("bad")
        return Call(status="ERROR" if self.fail else "SUCCESS", session_key=self.name + "-session",
                    structured_output=None if self.fail else self.output(),
                    usage={"total_tokens": self.tokens})

    def output(self):
        return {"hypotheses": [{"id": "h"}], "critiques": [], "summary": "ok"}

    def salvage(self, call, **kw):
        self.rescues.append(dict(kw, session=call["session_key"]))
        if self.salvage_leak:
            (kw["cwd"] / "leak.txt").write_text("bad salvage")
        rescued = Call(status="SUCCESS", structured_output=self.output(), usage={"total_tokens": 50})
        kw["on_attempt"](rescued)
        return rescued


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "target"
        self.target.mkdir()
        self.rd = self.root / "run"
        self.rd.mkdir()
        self.bindings = {r: FakeBackend(b, n) for r, b, n in zip(
            config.ROLES, ("agy", "codex", "claude"), (100, 200, 300))}
        self.cfg = dict(config.DEFAULTS, rounds=1, role_mode=True)
        self.logs, self.steps, self.ledger = [], [], Ledger()

    def one_round(self, dry_run=False, resume=False):
        return loop._one_round(1, 1, "t", self.target, self.cfg, [], self.rd, self.ledger,
                               backend=self.bindings["hypothesizer"], role_backends=self.bindings,
                               dry_run=dry_run, resume=resume, log=self.logs.append, steps=self.steps)


class TestRouting(Fixture):
    def test_routes_models_modes_and_verifier_notes(self):
        self.one_round()
        observed = [(b.name, b.calls[0]["mode"], b.calls[0]["model"]) for b in self.bindings.values()]
        self.assertEqual(observed, [("agy", MODE_READONLY, "agy-mid"),
                                    ("codex", MODE_READONLY, "codex-mid"),
                                    ("claude", MODE_WRITE, "claude-high")])
        prompt = self.bindings["verifier"].calls[0]["prompt"]
        self.assertIn("ENV-claude", prompt)
        self.assertNotIn("ENV-agy", prompt)
        self.assertNotIn("ENV-codex", prompt)

    def test_salvage_only_reaches_own_backend_and_session(self):
        self.bindings["challenger"].fail = True
        self.one_round()
        self.assertEqual([len(b.rescues) for b in self.bindings.values()], [0, 1, 0])
        rescue = self.bindings["challenger"].rescues[0]
        self.assertEqual((rescue["session"], rescue["model"], rescue["mode"]),
                         ("codex-session", "codex-mid", MODE_READONLY))
        self.assertEqual(self.ledger.total, 650)

    def test_dry_run_with_zero_partial_and_all_cache(self):
        for count in (0, 1, 3):
            self.logs.clear()
            for schema in ("hypotheses", "critique", "verification")[:count]:
                (self.rd / ("round1-" + schema + ".json")).write_text(json.dumps({"summary": "cached"}))
            self.one_round(dry_run=True, resume=True)
            plans = [s for s in self.logs if s.startswith("--- ")]
            self.assertEqual(len(plans), 3)
            self.assertEqual(sum("复用" in s for s in plans), count)
            for plan, b in zip(plans, self.bindings.values()):
                self.assertIn(b.name, plan)
            self.assertEqual(sum(len(b.calls) for b in self.bindings.values()), 0)

    def test_normal_and_salvage_guard_stop_following_roles(self):
        for salvage in (False, True):
            with self.subTest(salvage=salvage):
                self.bindings["challenger"].fail = salvage
                self.bindings["challenger"].leak = not salvage
                self.bindings["challenger"].salvage_leak = salvage
                with self.assertRaises(WorktreeChanged):
                    self.one_round()
                self.assertEqual(len(self.bindings["verifier"].calls), 0)
                (self.target / "leak.txt").unlink()
