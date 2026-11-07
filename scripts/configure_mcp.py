#!/usr/bin/env python3
"""AGANT Hermes remote MCP bootstrap. Additive and idempotent.

The optional AGANT_MCP_SERVERS_JSON is read from hosting environment variables.
Never commit keys to GitHub.
"""
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml

HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
CONFIG = HOME / "config.yaml"
STATE = HOME / ".agant-mcp-managed.json"
PREFIX = "agant_"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
TRUTHY = {"1", "true", "yes", "on"}


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def parse_custom_servers():
    raw = os.environ.get("AGANT_MCP_SERVERS_JSON", "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("AGANT_MCP_SERVERS_JSON must be a JSON object")
    custom = {}
    for name, details in parsed.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError("MCP server names must be lowercase letters, digits, _ or -")
        if name == "deepwiki":
            raise ValueError("'deepwiki' is reserved for the built-in test connection")
        if not isinstance(details, dict):
            raise ValueError(f"MCP server {name}: expected an object")
        url = details.get("url", "")
        if not isinstance(url, str):
            raise ValueError(f"MCP server {name}: url must be a string")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
            raise ValueError(f"MCP server {name}: a public HTTPS endpoint URL is required")
        allowed_keys = {"url", "headers", "tools", "enabled", "timeout", "connect_timeout"}
        if set(details) - allowed_keys:
            raise ValueError(f"MCP server {name}: unknown config keys")
        headers = details.get("headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError(f"MCP server {name}: headers must be a map of strings")
        tools = details.get("tools", {})
        if not isinstance(tools, dict):
            raise ValueError(f"MCP server {name}: tools must be an object")
        for field in ("include", "exclude"):
            if field in tools and not (
                isinstance(tools[field], list)
                and all(isinstance(item, str) for item in tools[field])
            ):
                raise ValueError(f"MCP server {name}: tools.{field} must be a list of strings")
        custom[PREFIX + name] = dict(details)
    return custom


def atomic_write_yaml(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".agant-mcp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(contents, handle, sort_keys=False, allow_unicode=True)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def main():
    # Validate new settings before changing any existing configuration.
    desired = parse_custom_servers()
    if os.environ.get("AGANT_MCP_DEEPWIKI", "true").strip().lower() in TRUTHY:
        desired[PREFIX + "deepwiki"] = {
            "url": "https://mcp.deepwiki.com/mcp",
            "enabled": True,
            "tools": {
                "include": ["read_wiki_structure", "read_wiki_contents", "ask_question", "ask_wiki_question"],
                "resources": False,
                "prompts": False,
            },
        }

    # Context7's public endpoint works with low anonymous rate limits.
    # A dedicated key can be supplied via hosting Variables for higher limits.
    if os.environ.get("AGANT_MCP_CONTEXT7", "true").strip().lower() in TRUTHY:
        context7 = {
            "url": "https://mcp.context7.com/mcp",
            "enabled": True,
            "tools": {
                "include": ["resolve-library-id", "query-docs"],
                "resources": False,
                "prompts": False,
            },
        }
        if os.environ.get("AGANT_MCP_CONTEXT7_API_KEY", "").strip():
            # Hermes interpolates this from the process environment at load time.
            # No secret value is serialized to config.yaml or committed to Git.
            context7["headers"] = {
                "Authorization": "Bearer ${AGANT_MCP_CONTEXT7_API_KEY}"
            }
        desired[PREFIX + "context7"] = context7

    # Never silently reuse the bot's possibly privileged existing GITHUB_TOKEN.
    # Activate only when a separate, least-privileged PAT is provided.
    if (
        os.environ.get("AGANT_MCP_GITHUB_TOKEN", "").strip()
        and os.environ.get("AGANT_MCP_GITHUB", "true").strip().lower() in TRUTHY
    ):
        desired[PREFIX + "github"] = {
            "url": "https://api.githubcopilot.com/mcp/readonly",
            "enabled": True,
            "headers": {
                "Authorization": "Bearer ${AGANT_MCP_GITHUB_TOKEN}",
                "X-MCP-Readonly": "true",
                "X-MCP-Toolsets": "repos,issues,pull_requests",
            },
            "tools": {"resources": False, "prompts": False},
        }

    previous = read_json(STATE, [])
    if not isinstance(previous, list):
        previous = []
    with CONFIG.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise ValueError("Hermes config.yaml is not a YAML mapping")
    servers = config.get("mcp_servers", {}) or {}
    if not isinstance(servers, dict):
        raise ValueError("Existing mcp_servers must be a map")

    # Preserve servers managed by Hermes or other components.
    servers = {name: data for name, data in servers.items() if name not in previous}
    servers.update(desired)
    if servers:
        config["mcp_servers"] = servers
    else:
        config.pop("mcp_servers", None)

    atomic_write_yaml(CONFIG, config)
    STATE.write_text(json.dumps(sorted(desired)) + "\n", encoding="utf-8")
    os.chmod(STATE, 0o600)
    print("[agant-mcp] Configured remote servers: " + (", ".join(sorted(desired)) or "none"))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, yaml.YAMLError):
        # Never print raw config, tokens, or endpoint credentials in logs.
        print("[agant-mcp] Configuration failed; existing bot config retained")
        raise SystemExit(1)
