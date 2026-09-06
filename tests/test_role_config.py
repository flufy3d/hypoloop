import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hypoloop import backends, cli, config


class TestRoleConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(config, "PROJECTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(config, "ensure_dirs")
        p.start()
        self.addCleanup(p.stop)

    def save(self, data):
        config.project_config_path(self.root).write_text(json.dumps(data), encoding="utf-8")

    def test_five_sources_and_shared_role_conflicts(self):
        for role in config.ROLES:
            key = role + "_model"
            self.save({})
            expected = (backends.get("agy").default_verifier_model if role == "verifier"
                        else backends.get("agy").default_model)
            self.assertEqual(config.load(self.root, backend="agy")["roles"][role]["model"], expected)
            saved = {"backend": "agy", key: "project"}
            self.save(saved)
            self.assertEqual(config.load(self.root, backend="agy")["roles"][role]["model"], "project")
            saved["backends"] = {"agy": {key: "bucket"}}
            self.save(saved)
            self.assertEqual(config.load(self.root, backend="agy")["roles"][role]["model"], "bucket")
            base = {"backend": "agy", key: "manifest"}
            self.assertEqual(config.load(self.root, base=base, backend="agy")["roles"][role]["model"], "manifest")
            self.assertEqual(config.load(self.root, {key: "cli"}, base, "agy")["roles"][role]["model"], "cli")
            # Higher-source shared settings override lower-source role settings.
            if role != "verifier":
                self.assertEqual(config.load(self.root, {"model": "shared"}, base, "agy")["roles"][role]["model"], "shared")
            result = config.load(self.root, {"backend": "codex"}, base, "codex")
            self.assertEqual(result["roles"][role]["backend"], "codex")
            self.assertNotIn(result["roles"][role]["model"], ("project", "bucket", "manifest"))

    def test_save_roundtrip_resume_and_switch(self):
        roles = {role: {"backend": name, "model": name + "-custom"}
                 for role, name in zip(config.ROLES, ("agy", "codex", "claude"))}
        config.save_project(self.root, {"roles": roles, "role_mode": True}, backend="agy")
        saved = json.loads(config.project_config_path(self.root).read_text())
        for role, pair in roles.items():
            self.assertEqual(saved["backends"][pair["backend"]][role + "_model"], pair["model"])
            self.assertNotIn(role + "_model", saved)
        cfg = config.load(self.root, backend="agy")
        self.assertEqual(cfg["roles"], roles)
        self.save({"backend": "claude", "model": "changed"})
        self.assertEqual(config.load(self.root, base=cfg, backend="claude")["roles"], roles)
        switched = config.load(self.root, {"verifier_backend": "agy"}, cfg, "agy")["roles"]
        self.assertEqual(switched["hypothesizer"], roles["hypothesizer"])
        self.assertEqual(switched["challenger"], roles["challenger"])
        self.assertEqual(switched["verifier"], {"backend": "agy", "model": backends.get("agy").default_verifier_model})

    def test_broken_containers(self):
        for value in ([], 7, {"backends": []}, {"backends": {"agy": []}}, {"roles": []}):
            self.save(value)
            self.assertEqual(len(config.load(self.root, backend="agy")["roles"]), 3)
            config.save_project(self.root, {"model": "ok"}, backend="agy")

    def test_cli_resume_preserves_pairs(self):
        roles = {r: {"backend": b, "model": b + "-custom"}
                 for r, b in zip(config.ROLES, ("agy", "codex", "claude"))}
        rd = self.root / "run"
        rd.mkdir()
        (rd / "run.json").write_text(json.dumps({"target": str(self.root), "task": "t",
            "config": {"backend": "agy", "roles": roles, "role_mode": True}}))
        with patch.object(cli, "run") as run, patch.object(backends, "installed", return_value=[]):
            self.assertEqual(cli.main(["-", "--resume", str(rd)]), 0)
            self.assertEqual(run.call_args.args[2]["roles"], roles)
            self.assertEqual(cli.main(["-", "--resume", str(rd), "--verifier-backend", "codex"]), 0)
            cfg = run.call_args.args[2]
            self.assertEqual(cfg["roles"]["hypothesizer"], roles["hypothesizer"])
            self.assertEqual(cfg["roles"]["verifier"]["model"], backends.get("codex").default_verifier_model)
