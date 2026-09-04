"""抢救那一步也必须在只读指纹里面。

漏掉的话，只读角色的续接调用就成了整套系统里唯一一个没人看着的写入口。
"""

from pathlib import Path
import tempfile
import unittest

from hypoloop import loop
from hypoloop.backends.base import MODE_READONLY, MODE_WRITE, Backend, Call
from hypoloop.guard import WorktreeChanged
from hypoloop.ledger import Ledger


class _FailThenLeak(Backend):
    """第一次调用必失败；抢救时往工作区里写一个文件。"""

    name = "fake"

    def __init__(self, target: Path, leak_name: str):
        self.target = target
        self.leak_name = leak_name

    def binary(self):
        return "fake"

    def run(self, prompt, **kw):
        return Call(status="ERROR", conversation_id="conv-fail")

    def salvage(self, call, **kw):
        (self.target / self.leak_name).write_text("written by salvage",
                                                  encoding="utf-8")
        return Call(status="SUCCESS",
                    structured_output={"summary": "ok", "hypotheses": [],
                                       "changes": []})


def _dirs(td):
    root = Path(td)
    target, run_dir = root / "target", root / "run"
    target.mkdir()
    run_dir.mkdir()
    return target, run_dir


class TestSalvageReadOnlyGuard(unittest.TestCase):
    def test_salvage_under_readonly_guard_raises_on_worktree_change(self):
        with tempfile.TemporaryDirectory() as td:
            target, run_dir = _dirs(td)
            backend = _FailThenLeak(target, "leak.txt")

            with self.assertRaises(WorktreeChanged) as ctx:
                loop._step(
                    "假设者", "test prompt", target, {}, run_dir, Ledger(), 1,
                    backend, model="m", mode=MODE_READONLY,
                    schema_name="hypotheses", timeout=10, readonly=True,
                    dry_run=False, log=lambda s: None)

            self.assertIn("假设者(续接)", str(ctx.exception))
            self.assertIn("只读角色", str(ctx.exception))

    def test_verifier_salvage_allows_worktree_change(self):
        with tempfile.TemporaryDirectory() as td:
            target, run_dir = _dirs(td)
            backend = _FailThenLeak(target, "valid.txt")

            result = loop._step(
                "验证者", "test prompt", target, {}, run_dir, Ledger(), 1,
                backend, model="m", mode=MODE_WRITE,
                schema_name="verification", timeout=10, readonly=False,
                dry_run=False, log=lambda s: None)

            self.assertEqual(result.get("summary"), "ok")
            self.assertTrue((target / "valid.txt").exists())
