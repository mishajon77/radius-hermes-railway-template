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
        for key in (
            "AGANT_MCP_SERVERS_JSON", "AGANT_MCP_DEEPWIKI",
            "AGANT_MCP_CONTEXT7", "AGANT_MCP_CONTEXT7_API_KEY",
            "AGANT_MCP_GITHUB", "AGANT_MCP_GITHUB_TOKEN",
        ):
            env.pop(key, None)
        env.update({"HERMES_HOME": str(self.home), **settings})
        return subprocess.run(
            [sys.executable, str(SCRIPT)], env=env,
            capture_output=True, text=True, check=False
        )

    def names(self):
        return set(yaml.safe_load(self.file.read_text(encoding="utf-8"))["mcp_servers"])

    def test_default_and_disable(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(self.names(), {"agant_deepwiki", "agant_context7", "existing"})
        self.assertEqual(self.run_bootstrap(AGANT_MCP_DEEPWIKI="false").returncode, 0)
        self.assertEqual(self.names(), {"agant_context7", "existing"})
        self.assertEqual(self.run_bootstrap(AGANT_MCP_DEEPWIKI="false", AGANT_MCP_CONTEXT7="false").returncode, 0)
        self.assertEqual(self.names(), {"existing"})

    def test_custom_server(self):
        settings = json.dumps({"docs": {"url": "https://docs.example/mcp"}})
        self.assertEqual(self.run_bootstrap(AGANT_MCP_SERVERS_JSON=settings).returncode, 0)
        self.assertEqual(self.names(), {"agant_deepwiki", "agant_context7", "agant_docs", "existing"})
        self.assertEqual(yaml.safe_load(self.file.read_text(encoding="utf-8"))["model"], "some-model")

    def test_context7_uses_env_reference_not_embedded_key(self):
        secret = "test-context7-key-do-not-serialize"
        result = self.run_bootstrap(AGANT_MCP_CONTEXT7_API_KEY=secret)
        self.assertEqual(result.returncode, 0)
        text = self.file.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn(secret, result.stdout + result.stderr)
        config = yaml.safe_load(text)
        self.assertEqual(
            config["mcp_servers"]["agant_context7"]["headers"]["Authorization"],
            "Bearer ${AGANT_MCP_CONTEXT7_API_KEY}",
        )
        self.assertEqual(
            config["mcp_servers"]["agant_context7"]["tools"]["include"],
            ["resolve-library-id", "query-docs"],
        )

    def test_github_requires_separate_token_and_stays_readonly(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        self.assertNotIn("agant_github", self.names())
        secret = "test-github-pat-do-not-serialize"
        result = self.run_bootstrap(AGANT_MCP_GITHUB_TOKEN=secret)
        self.assertEqual(result.returncode, 0)
        config = yaml.safe_load(self.file.read_text(encoding="utf-8"))
        github = config["mcp_servers"]["agant_github"]
        self.assertEqual(github["url"], "https://api.githubcopilot.com/mcp/readonly")
        self.assertEqual(github["headers"]["X-MCP-Readonly"], "true")
        self.assertEqual(github["headers"]["X-MCP-Toolsets"], "repos,issues,pull_requests")
        self.assertEqual(github["headers"]["Authorization"], "Bearer ${AGANT_MCP_GITHUB_TOKEN}")
        self.assertNotIn(secret, self.file.read_text(encoding="utf-8"))
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(
            self.run_bootstrap(AGANT_MCP_GITHUB_TOKEN=secret, AGANT_MCP_GITHUB="false").returncode, 0
        )
        self.assertNotIn("agant_github", self.names())

    def test_invalid_url_keeps_previous_config(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        before = self.file.read_bytes()
        bad = json.dumps({"bad": {"url": "http://not-https.example/mcp"}})
        self.assertNotEqual(self.run_bootstrap(AGANT_MCP_SERVERS_JSON=bad).returncode, 0)
        self.assertEqual(self.file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
