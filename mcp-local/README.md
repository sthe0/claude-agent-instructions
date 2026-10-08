# Local MCP servers (`mcp-local/`)

Directory is **not versioned** (except this README).

Each `<name>.json` file is a config for one MCP server:

```json
{
  "command": "npx",
  "args": ["-y", "@some/mcp-server"],
  "env": { "KEY": "value" }
}
```

Register all configs into `$CLAUDE_AGENT_HOME/.claude.json` (user scope, the file Claude Code reads servers from):

```bash
~/claude-agent-instructions/scripts/apply-mcp-local.sh
```

The script is idempotent: re-running updates existing entries without duplicating them. Values (e.g. `env`) are never printed. An org layer may also install definitions in `${CLAUDE_MCP_PLUGIN_DIR:-<config root>/mcp-plugins}/<name>.json` (the `mcp-plugins` seam); on a name clash `mcp-local/` wins. `mcpServers` left in `settings.local.json` by the old script are ignored by Claude Code.

To restore on a new machine: copy the needed `*.json` files from another machine or a backup, then run `apply-mcp-local.sh`.
