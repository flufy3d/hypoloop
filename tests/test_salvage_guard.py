from pathlib import Path
import tempfile
import unittest

from hypoloop import loop
from hypoloop.agy import AgyCall
from hypoloop.guard import WorktreeChanged
from hypoloop.ledger import Ledger


class TestSalvageReadOnlyGuard(unittest.TestCase):
    def setUp(self):
        self._orig_run_agy = loop.run_agy
        self._orig_salvage = loop.salvage

    def tearDown(self):
        loop.run_agy = self._orig_run_agy
        loop.salvage = self._orig_salvage

    def test_salvage_under_readonly_guard_raises_on_worktree_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            target.mkdir()
            run_dir = root / "run"
            run_dir.mkdir()

            loop.run_agy = lambda *a, **kw: AgyCall(
                status="ERROR", conversation_id="conv-fail-1")

            def mock_salvage(*a, **kw):
                (target / "leak.txt").write_text("illegal file from salvage", encoding="utf-8")
                return AgyCall(status="SUCCESS",
                               structured_output={"summary": "ok", "hypotheses": []})

            loop.salvage = mock_salvage

            with self.assertRaises(WorktreeChanged) as ctx:
                loop._step(
                    "假设者", "test prompt", target, {}, run_dir, Ledger(), 1,
                    model="m", mode="plan", schema_name="hypotheses",
                    timeout=10, readonly=True, dry_run=False, log=lambda s: None)

            self.assertIn("假设者(续接)", str(ctx.exception))
            self.assertIn("只读角色", str(ctx.exception))

    def test_verifier_salvage_allows_worktree_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            target.mkdir()
            run_dir = root / "run"
            run_dir.mkdir()

            loop.run_agy = lambda *a, **kw: AgyCall(
                status="ERROR", conversation_id="conv-fail-2")

            def mock_salvage(*a, **kw):
                (target / "valid.txt").write_text("allowed file from verifier", encoding="utf-8")
                return AgyCall(status="SUCCESS",
                               structured_output={"summary": "ok", "changes": []})

            loop.salvage = mock_salvage

            result = loop._step(
                "验证者", "test prompt", target, {}, run_dir, Ledger(), 1,
                model="m", mode="accept-edits", schema_name="verification",
                timeout=10, readonly=False, dry_run=False, log=lambda s: None)

            self.assertEqual(result.get("summary"), "ok")
            self.assertTrue((target / "valid.txt").exists())
