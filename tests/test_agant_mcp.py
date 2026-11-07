"""Smoke tests for the AGANT remote MCP configuration helper.

Run: python -m unittest discover -s tests -p 'test_agant_mcp.py'
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "configure_mcp.py"


class MCPBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.file = self.home / "config.yaml"
        self.file.write_text(yaml.safe_dump({
            "model": "some-model",
            "mcp_servers": {"existing": {"url": "https://existing.example/mcp"}},
        }), encoding="utf-8")

    def run_bootstrap(self, **settings):
        env = os.environ.copy()
        env.pop("AGANT_MCP_SERVERS_JSON", None)
        env.pop("AGANT_MCP_DEEPWIKI", None)
        env.update({"HERMES_HOME": str(self.home), **settings})
        return subprocess.run(
            [sys.executable, str(SCRIPT)], env=env,
            capture_output=True, text=True, check=False
        )

    def names(self):
        return set(yaml.safe_load(self.file.read_text(encoding="utf-8"))["mcp_servers"])

    def test_default_and_disable(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(self.names(), {"agant_deepwiki", "existing"})
        self.assertEqual(self.run_bootstrap(AGANT_MCP_DEEPWIKI="false").returncode, 0)
        self.assertEqual(self.names(), {"existing"})

    def test_custom_server(self):
        settings = json.dumps({"docs": {"url": "https://docs.example/mcp"}})
        self.assertEqual(self.run_bootstrap(AGANT_MCP_SERVERS_JSON=settings).returncode, 0)
        self.assertEqual(self.names(), {"agant_deepwiki", "agant_docs", "existing"})
        self.assertEqual(yaml.safe_load(self.file.read_text(encoding="utf-8"))["model"], "some-model")

    def test_invalid_url_keeps_previous_config(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        before = self.file.read_bytes()
        bad = json.dumps({"bad": {"url": "http://not-https.example/mcp"}})
        self.assertNotEqual(self.run_bootstrap(AGANT_MCP_SERVERS_JSON=bad).returncode, 0)
        self.assertEqual(self.file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
