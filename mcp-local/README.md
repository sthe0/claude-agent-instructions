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

Apply all configs to `~/.claude-agent/settings.local.json`:

```bash
~/claude-agent-instructions/scripts/apply-mcp-local.sh
```

The script is idempotent: re-running updates existing entries without duplicating them.

To restore on a new machine: copy the needed `*.json` files from another machine or a backup, then run `apply-mcp-local.sh`.
