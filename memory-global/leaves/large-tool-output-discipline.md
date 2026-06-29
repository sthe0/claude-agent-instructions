---
name: large-tool-output-discipline
description: When a single tool call (Bash, MCP query, file Read of a huge file) is about to return more than ~4 KB of content, pipe it through scripts/offload-large.sh so the context window receives a head+tail digest and the full bytes live in /tmp/cc-scratch/. Reading the digest is the fast path; Read(offset, limit) on the scratch file is the escape hatch.
type: reference
created: 2026-05-27
last_verified: 2026-05-27
---

# Large tool-output discipline

The model's context window pays full price for every byte a tool returns. Observed in 2026-05-27 deepagent sessions: single Tracker comment responses of 14 KB, plan-file Reads of 25 KB, Nirvana instance JSONs of 24 KB — each landing in cache as a permanent fixture of the conversation prefix. See [token-economy-plan.md](token-economy-plan.md) item 5 and Anthropic's [tool-clearing Cookbook recipe](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools).

## When to wrap a command

Apply the wrapper proactively before running:

- A `find` / `rg` / `arc grep` / `ya tool grep` that might match many files.
- A log tail or `juggler` / `monium` query that could return hundreds of lines.
- A `tracker-cli.sh` / `arcanum-cli.sh` / similar JSON dump where the model needs a few fields, not the entire blob.
- A `gh pr view --comments` / `arc pr show` on a heavily-discussed PR.
- A `docker logs` / `kubectl logs` invocation.

Heuristic: if you would normally pipe through `head -50` to keep the context clean — instead pipe through `offload-large.sh`, which keeps the head **and** lets you go back for the tail.

If you've already issued a command and the result was big (you see the cost in the next tool result) — the leak is already paid. Switch to the wrapper for the next call; do not re-run.

## How to use

```bash
<command> | ~/claude-agent-instructions/scripts/offload-large.sh
<command> 2>&1 | ~/claude-agent-instructions/scripts/offload-large.sh
```

Tunables (env vars; defaults shown):

| Variable | Default | Effect |
|---|---|---|
| `OFFLOAD_THRESHOLD_BYTES` | 4096 | Below this, pass through unchanged. |
| `OFFLOAD_HEAD_LINES` | 40 | Lines kept at the top of the digest. |
| `OFFLOAD_TAIL_LINES` | 20 | Lines kept at the bottom of the digest. |
| `OFFLOAD_SCRATCH_DIR` | `/tmp/cc-scratch` | Where the full output is parked. |

Below the threshold the script is a `cat`; above, it prints head + truncation banner + tail + a `Full output at:` line.

## When you need the full bytes

The wrapper prints a `Full output at: <path>` line. Use `Read(file_path=<path>, offset=N, limit=M)` to pull specific slices — typically a single error-context window — without dumping the whole file back into context.

## What this doesn't fix

- **File Reads of known-large files.** The wrapper sits on Bash stdout; `Read(file_path)` does not go through it. For known-large files use `Read` with `offset`/`limit` directly, or read a small subset via Bash (`sed -n '100,150p' <file> | offload-large.sh`).
- **MCP tool outputs.** Those are returned directly to the model by Claude Code's MCP client; the wrapper cannot intercept them. The mitigation there is server-side filtering (e.g., `mcp__tracker__GetIssue` with field projection where the tool supports it) — see specific tool docs.
- **The `Wasted call` short-circuit for unchanged files.** The harness already protects Read of unchanged files ([system-knowledge/harness-read-dedup.md](system-knowledge/harness-read-dedup.md)). The wrapper is for *new* outputs that haven't been seen yet.

## Why this is discipline, not a hook

A PostToolUse hook can emit stderr but does **not** replace the tool_result content the model sees. Auto-replacing Bash output via the harness would require a wrapper at the shell layer, which is not how Claude Code invokes Bash. The wrapper-by-pipe pattern keeps control with the agent and is opt-in per call.
