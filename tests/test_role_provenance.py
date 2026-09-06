import tempfile
import unittest
from pathlib import Path

from hypoloop import loop, report
from hypoloop.backends import Backend, Call, MODE_READONLY
from hypoloop.ledger import Ledger
from hypoloop.quota import unavailable


class Fake(Backend):
    name = "agy"

    def run(self, prompt, **kw):
        return Call(status="SUCCESS", structured_output={"hypotheses": [{"id": "h"}]},
                    usage={"total_tokens": 100})


class TestProvenance(unittest.TestCase):
    def test_source_survives_two_resumes_and_assignment_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            target.mkdir()
            backend = Fake()
            def step(model, resume, steps):
                ledger = Ledger()
                loop._step("假设者", "p", target, {}, root, ledger, 1, backend,
                           model=model, mode=MODE_READONLY, schema_name="hypotheses",
                           timeout=1, readonly=True, dry_run=False, resume=resume,
                           log=lambda s: None, steps=steps)
                return ledger.total
            self.assertEqual(step("A", False, []), 100)
            backend.name = "codex"
            for _ in range(2):
                steps = []
                self.assertEqual(step("B", True, steps), 0)
                self.assertEqual(steps[0]["source"], {"backend": "agy", "model": "A"})
                q = unavailable("test")
                path = report.write_report(root, task="t", target=target, rounds=[],
                                           ledger_text="", quota_before=q, quota_after=q,
                                           consumed={}, steps=steps)
                text = path.read_text(encoding="utf-8")
                self.assertIn("产出 agy/A", text)
                self.assertIn("当前指派 codex/B", text)
            (root / "round1-hypotheses.json").write_text('{"hypotheses": []}')
            steps = []
            step("B", True, steps)
            self.assertEqual(steps[0]["source"], {})

    def test_legacy_cache_has_unknown_source(self):
        with tempfile.TemporaryDirectory() as td:
            cached = Path(td) / "round1-hypotheses.json"
            cached.write_text("{}")
            self.assertEqual(loop._cached_source(cached), {})
