---
name: cursor-agent-cli-spawn
description: Cursor Agent CLI (agent -p) for headless spawns in Cursor; API key file and install on Linux
type: reference
---

# Cursor Agent CLI spawn (Cursor sessions)

Use when overcome-difficulty needs a **fresh manager** in Cursor and `claude -p` is forbidden.

## Install (Linux)

The install URL serves **gzip-compressed** script — pipe through `gunzip`:

```bash
curl -fsSL https://cursor.com/install | gunzip | bash
export PATH="$HOME/.local/bin:$PATH"
agent --version
```

Binary: `~/.local/bin/agent` (symlink `cursor-agent` may also exist).

## API key

- User key file: `~/.cursor_api_key` (one line, no trailing newline required).
- Env override: `CURSOR_API_KEY`.
- Wrapper: `~/claude-agent-instructions/scripts/spawn-cursor-escape.py` reads the file by default (`--api-key-file`).

Do not commit or log the key.

## Headless smoke

```bash
export CURSOR_API_KEY="$(tr -d '\n' < ~/.cursor_api_key)"
# Direct CLI
timeout 120 agent -p "Reply with exactly one line: RESOLVED: ping" \
  --trust --force --approve-mcps \
  --workspace /path/to/project \
  --output-format text --model composer-2.5
# Wrapper plumbing check
~/claude-agent-instructions/scripts/spawn-cursor-escape.py --smoke --workspace /path/to/project
```

Flags for unattended: `--trust`, `--force`, `--approve-mcps`.

## Wrapper

`spawn-cursor-escape.py` — recursion cap, overcome-difficulty prompt, marker validation, `~/.local/log/cursor-spawn-costs.jsonl`.

> verified by: smoke on the0 2026-06-03 — `agent` 2026.06.02-8c11d9f, exit 0, `RESOLVED: ping`.
