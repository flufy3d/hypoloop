import json
from pathlib import Path
import tempfile
import unittest

from hypoloop import cli, config


class TestResumeManifestConfig(unittest.TestCase):
    def test_resume_restores_manifest_config(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run1"
            run_dir.mkdir()
            target_dir = Path(td) / "target"
            target_dir.mkdir()

            orig_cfg = {
                "stop_when": "全速度段必死率为 0",
                "rounds": 5,
                "verifier_model": "custom-verifier",
                "evidence_hint": "hint text",
            }
            manifest = {
                "task": "original task",
                "target": str(target_dir),
                "config": orig_cfg,
            }
            (run_dir / "run.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            captured_cfg = {}
            orig_run = cli.run

            def mock_run(task, target, cfg, **kw):
                captured_cfg.update(cfg)
                return {}

            cli.run = mock_run
            try:
                cli.main(["--resume", str(run_dir), "-"])
            finally:
                cli.run = orig_run

            self.assertEqual(captured_cfg.get("stop_when"), "全速度段必死率为 0")
            self.assertEqual(captured_cfg.get("rounds"), 5)
            self.assertEqual(captured_cfg.get("verifier_model"), "custom-verifier")
            self.assertEqual(captured_cfg.get("evidence_hint"), "hint text")

    def test_resume_cli_overrides_manifest_config(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run2"
            run_dir.mkdir()
            target_dir = Path(td) / "target2"
            target_dir.mkdir()

            orig_cfg = {
                "stop_when": "original stop",
                "rounds": 5,
            }
            manifest = {
                "task": "original task",
                "target": str(target_dir),
                "config": orig_cfg,
            }
            (run_dir / "run.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            captured_cfg = {}
            orig_run = cli.run

            def mock_run(task, target, cfg, **kw):
                captured_cfg.update(cfg)
                return {}

            cli.run = mock_run
            try:
                cli.main(["--resume", str(run_dir), "-", "--rounds", "8"])
            finally:
                cli.run = orig_run

            self.assertEqual(captured_cfg.get("rounds"), 8)
            self.assertEqual(captured_cfg.get("stop_when"), "original stop")
