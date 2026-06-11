---
name: cursor-agent-cli-spawn
description: Difficulty it removes — you need a headless agent spawn inside Cursor where claude -p is unavailable. Fact — Cursor Agent CLI (agent -p), API key file ~/.cursor_api_key, Linux install via curl|gunzip|bash, wrapped by spawn-cursor-escape.py.
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

## Wrappers

| Script | Use |
|---|---|
| `spawn-cursor-specialist.py` | Specialization spawn (`--kind planner\|developer\|…`): inline SKILL.md + plan/done-criterion prompt, budget tier → timeout (300/600/900s), same return markers as `spawn-specialist.py` |
| `spawn-cursor-escape.py` | Overcome-difficulty recursive escape: `RESOLVED:` / `INVESTIGATION:` / `LOOP_DETECTED:` |

Both: recursion cap from `config.md`, `CURSOR_API_KEY` from env or `~/.cursor_api_key`, cost log `~/.local/log/cursor-spawn-costs.jsonl`, `--dry-run`, optional `--smoke`.

```bash
# Specialist dry-run (no API call)
~/claude-agent-instructions/scripts/spawn-cursor-specialist.py \
  --kind developer --plan /path/to/plan.md \
  --done-criterion "…" --criterion-type measurable \
  --workspace /path/to/project --dry-run
# Specialist smoke
~/claude-agent-instructions/scripts/spawn-cursor-specialist.py --smoke --workspace /path/to/project
```

> verified by: smoke on the0 2026-06-03 — `agent` 2026.06.02-8c11d9f, exit 0, `RESOLVED: ping` (escape), `COMPLETED: ping` (specialist).
