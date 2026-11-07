# AGANT Hermes — Remote MCP starter

This patch adds optional remote MCP support to the existing pinned Hermes release while preserving the Telegram, OpenRouter and model configuration.

## First deploy

- The Docker image installs the upstream `mcp` extra alongside `cron` and `pty`.
- On each agent startup, `scripts/configure_mcp.py` adds a public, read-only-oriented DeepWiki connection to `HERMES_HOME/config.yaml`.
- The egress-only role exits before the agent configuration step.
- Existing MCP entries not managed by AGANT are preserved.

The default connection is `agant_deepwiki` at `https://mcp.deepwiki.com/mcp`. Set `AGANT_MCP_DEEPWIKI=false` in your hosting service's Variables to disable it.

## Test in Telegram

After the image successfully builds and redeploys, ask:

```
Use the DeepWiki MCP tools to list the documentation sections of NousResearch/hermes-agent, and tell me which MCP tool you called.
```

The startup log line `[agant-mcp] Configured remote servers: agant_deepwiki` indicates successful configuration file generation, **not** a confirmed MCP connection. Verify a successful real tool call.

## More remote servers

Add `AGANT_MCP_SERVERS_JSON` as a hosting-only environment variable:

```json
{
  "my_service": {
    "url": "https://YOUR-EXISTING-SERVER.example/mcp",
    "enabled": true
  }
}
```

`my_service` becomes `agant_my_service`. Multiple named remote endpoints may be configured in the same JSON object. Only public HTTPS URLs are accepted by this bootstrap. Do not commit tokens, URLs containing credentials or secrets to GitHub. If your server requires authentication, configure headers in a secure hosting environment variable, not in the repository.

## Safety and rollback

- Start with read-only or minimally privileged MCP servers. Do not add arbitrary or untrusted remote endpoints to a privileged agent.
- Review your hosting build logs and resource use; the MCP client adds dependencies and remote sessions.
- To roll back, close this PR without merging, or revert the merge commit if already deployed.
- The agent's normal chat adapters and provider credentials are not altered by this patch.
