# AGANT Hermes — Remote MCP

AGANT extends the existing pinned Hermes installation with hosted MCP servers and preserves Telegram, OpenRouter, model settings, and unrelated MCP entries.

## Built-in servers

| Server | Name in Hermes | Default | Notes |
| --- | --- | --- | --- |
| DeepWiki | `agant_deepwiki` | On | Source-oriented GitHub repository docs lookup |
| Context7 | `agant_context7` | On | Current development docs; low unauthenticated rate limit |
| GitHub | `agant_github` | Off until a separate token is provided | Hosted **read-only** GitHub MCP, only repos/issues/pull_requests toolsets |

All three use remote HTTPS MCP connections, so the container does not launch any additional MCP subprocesses.

## Hostless / hosting Variables

These are **optional**, and must be set in the hosting provider's secret Variables panel, not in GitHub:

| Variable | Meaning |
| --- | --- |
| `AGANT_MCP_DEEPWIKI=false` | Disable DeepWiki |
| `AGANT_MCP_CONTEXT7=false` | Disable Context7 |
| `AGANT_MCP_CONTEXT7_API_KEY` | Optional Context7 key, for higher limits |
| `AGANT_MCP_GITHUB_TOKEN` | Dedicated scoped GitHub PAT; adding this activates read-only GitHub MCP |
| `AGANT_MCP_GITHUB=false` | Disable GitHub MCP even if token is configured |
| `AGANT_MCP_SERVERS_JSON` | JSON object with further named remote HTTPS MCP servers |

**Do not use the bot's existing `GITHUB_TOKEN` by default.** Prefer a fresh fine-grained read-only GitHub PAT limited to repositories the bot should inspect, with Metadata, Contents, Issues, and Pull Requests read access as needed. GitHub connection to ChatGPT is a **separate** integration; it does not authenticate the remote Hermes bot.

Hermes expands `${AGANT_MCP_GITHUB_TOKEN}` and `${AGANT_MCP_CONTEXT7_API_KEY}` from its runtime environment. The generated `config.yaml` contains the variable expressions rather than plaintext secrets. Never paste tokens into Telegram, GitHub source, GitHub Issues or chat.

GitHub MCP remote endpoint is `https://api.githubcopilot.com/mcp/readonly` with `X-MCP-Readonly: true` and `X-MCP-Toolsets: repos,issues,pull_requests`. The remote endpoint filters out write tools. API token permissions are the additional access boundary.

## Test in Telegram

After a successful new image deployment:

1. Send `/reload-mcp` (or start a fresh Hermes session).
2. Test DeepWiki: "Use DeepWiki MCP to list documentation sections for NousResearch/hermes-agent. Name the exact tool."
3. Test Context7: "Use Context7 MCP to resolve the Python FastAPI library ID and find documentation for creating a GET endpoint. Name the MCP tools used."
4. Once your dedicated GitHub PAT is configured, ask: "Use the GitHub MCP to read the README in my selected repository. Do not create or change anything. Name the exact tool."

`[agant-mcp] Configured remote servers` in startup logs means the configuration was written **not** that the remote servers connected. Check tool use in responses and MCP/gateway logs.

## Additional remote servers

Set `AGANT_MCP_SERVERS_JSON` in hosting Variables, for example:

```json
{
  "my_service": {
    "url": "https://YOUR-EXISTING-SERVER.example/mcp",
    "enabled": true,
    "tools": {
      "include": ["query"],
      "resources": false,
      "prompts": false
    }
  }
}
```

The key becomes `agant_my_service`. These configs are additive; unowned MCP entries are preserved. Keep credentials in hosting Variables. Exposing many MCP tools costs model context and can increase latency, so enable only useful servers and use tool allowlists for privileged systems.

## Maintenance, safety and rollback

- This patch only changes config on agent boot. The egress-only service exits before MCP configuration.
- On a host without persistent volume, the config is regenerated every time from the image and hosting Variables.
- To rollback: revert the relevant merge commit or disable built-in servers with environment variables. `/reload-mcp` refreshes after config changes; a new container deployment also reconnects.
- The current setup does not expose arbitrary repo write access via GitHub MCP.
- A full production build, hosting deployment and remote handshake must be verified separately; unit tests alone do not guarantee service availability.

Official references:
- Context7: https://context7.com/docs/resources/all-clients
- GitHub MCP: https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md
- Hermes: https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference/
