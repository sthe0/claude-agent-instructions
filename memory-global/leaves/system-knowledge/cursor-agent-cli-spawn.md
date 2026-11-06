---
name: cursor-agent-cli-spawn
description: Difficulty it removes — you need a headless agent spawn inside Cursor where claude -p is unavailable. Fact — Cursor Agent CLI (agent -p), API key file ~/.cursor_api_key, Linux install via curl|gunzip|bash, wrapped by spawn-cursor-escape.py.
type: reference
schema: regularity/v1
converted_from: leaf/v1
given: code
given_source: scripts/spawn-cursor-escape.py
created: 2026-06-03
last_verified: 2026-07-30
---

# Cursor Agent CLI spawn (Cursor sessions)

## Difficulty

You need a headless agent spawn inside Cursor (e.g. `overcome-difficulty` recursive escape) but `claude -p` is unavailable — the Cursor sandbox does not expose the Claude Code CLI. A plain `claude -p` call fails with command-not-found or permission denied.

## Regularity

Inside Cursor, where the Claude Code CLI is not exposed, a headless agent spawn goes through the Cursor Agent CLI: on Linux it is installed with "curl -fsSL https://cursor.com/install | gunzip | bash" (the install URL serves a gzip-compressed script), giving the binary ~/.local/bin/agent (with possibly a cursor-agent symlink). The API key is read from the env var CURSOR_API_KEY or the file ~/.cursor_api_key, and unattended runs take the flags --trust, --force and --approve-mcps. scripts/spawn-cursor-escape.py wraps the overcome-difficulty recursive escape (returning RESOLVED:, INVESTIGATION: or LOOP_DETECTED:) and scripts/spawn-cursor-specialist.py wraps specialization spawns with the same return markers; both enforce the recursion cap from config.md and write a cost log at ~/.local/log/cursor-spawn-costs.jsonl. A Cursor-bound agentctl session (runtime_host=cursor) dispatches spawn stages through spawn-cursor-specialist.py, and the wrappers refuse when AGENTCTL_RUNTIME_HOST names the other host. A smoke on 2026-07-30 with agent 2026.07.23-e383d2b exited 0 with "RESOLVED: ping".

## Given

Given from code: `scripts/spawn-cursor-escape.py`. The statement is taken as known from that source, not induced from recorded experience.

## Refutation

Running the install pipeline without gunzip successfully, finding no CURSOR_API_KEY or ~/.cursor_api_key reading in scripts/spawn-cursor-escape.py, or a wrapper smoke on a current agent version failing to return the marker, would show the statement false.

## Guidance

Use when `overcome-difficulty` needs a **fresh manager** in Cursor and `claude -p` is forbidden.

### Install (Linux)

The install URL serves **gzip-compressed** script — pipe through `gunzip`:

```bash
curl -fsSL https://cursor.com/install | gunzip | bash
export PATH="$HOME/.local/bin:$PATH"
agent --version
```

Binary: `~/.local/bin/agent` (symlink `cursor-agent` may also exist).

### API key

- User key file: `~/.cursor_api_key` (one line, no trailing newline required).
- Env override: `CURSOR_API_KEY`.
- Wrapper: `~/claude-agent-instructions/scripts/spawn-cursor-escape.py` reads the file by default (`--api-key-file`).

Do not commit or log the key.

### Headless smoke

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


### agentctl (`runtime_host=cursor`)

A Cursor-bound `agentctl` session (`--host cursor` on `start`/`classify`, sticky `state.runtime_host`) dispatches spawn stages via `spawn-cursor-specialist.py`, not `spawn-specialist.py`. Advisor / marker_extract short calls use `lib.host_llm` → `agent -p` (omit `--model` for Auto). Wrappers refuse when `AGENTCTL_RUNTIME_HOST` names the other host. No cross-CLI fallback.

### Wrappers

| Script | Use |
|---|---|
| `spawn-cursor-specialist.py` | Specialization spawn (`--complexity` / `--model`, `--stage-index`, `--continue-worktree` (`--kind planner\|developer\|…`): inline SKILL.md + plan/done-criterion prompt, budget tier → timeout (300/600/900s), same return markers as `spawn-specialist.py` |
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

> verified by: smoke on the0 2026-07-30 — `agent` 2026.07.23-e383d2b, exit 0, `RESOLVED: ping` (escape; Cursor wrappers use legacy line-start marker scan — they do not invoke `claude`-backed `marker_extract`). Specialist smoke: run `--smoke` after install.

## See also
