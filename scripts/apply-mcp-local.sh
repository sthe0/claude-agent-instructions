#!/usr/bin/env bash
# Merge mcp-local/*.json into ~/.claude/settings.local.json under mcpServers.
# Idempotent: re-running updates existing entries.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MCP_DIR="$REPO/mcp-local"
# shellcheck source=scripts/lib/config-root.sh
source "$REPO/scripts/lib/config-root.sh"  # exports CLAUDE_AGENT_HOME (system root)
SETTINGS="${CLAUDE_SETTINGS_LOCAL:-$CLAUDE_AGENT_HOME/settings.local.json}"  # system root (isolated or legacy)

if [[ ! -d "$MCP_DIR" ]]; then
  echo "mcp-local/ not found, nothing to do"
  exit 0
fi

shopt -s nullglob
files=("$MCP_DIR"/*.json)
if [[ ${#files[@]} -eq 0 ]]; then
  echo "No *.json files in mcp-local/, nothing to do"
  exit 0
fi

if [[ ! -f "$SETTINGS" ]]; then
  echo '{}' > "$SETTINGS"
fi

python3 - "$SETTINGS" "${files[@]}" <<'EOF'
import json, sys, os

settings_path = sys.argv[1]
mcp_files = sys.argv[2:]

with open(settings_path) as f:
    settings = json.load(f)

settings.setdefault("mcpServers", {})

for path in mcp_files:
    name = os.path.splitext(os.path.basename(path))[0]
    with open(path) as f:
        cfg = json.load(f)
    settings["mcpServers"][name] = cfg
    print(f"  applied: {name}")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
    f.write("\n")
EOF

echo "Done. Updated $SETTINGS"
