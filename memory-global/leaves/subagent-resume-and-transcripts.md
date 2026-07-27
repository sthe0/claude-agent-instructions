---
name: subagent-resume-and-transcripts
description: How to resume a previously spawned Claude Code subagent (SendMessage + agent ID, requires CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1) and where subagent transcripts live on disk. Two operational nuances — subagent auto-compaction and transcript cleanup — that matter for long-running or post-mortem work.
type: reference
created: 2026-05-22
last_verified: 2026-05-22
---

# Subagent: resume and transcripts

Reference for the resume mechanism and the on-disk transcripts of Claude Code subagents.

## Resume

Each `Task` invocation creates a new subagent with a fresh context. To continue an existing subagent's work instead of starting over, use `SendMessage` with the agent's ID as the `to` field.

- Resumed subagent retains its **full history** — prior tool calls, results, reasoning — and picks up where it stopped, not from a clean slate.
- A stopped subagent that receives `SendMessage` **auto-resumes in the background**, no fresh `Task` call required.
- The agent ID is returned to the parent when `Task` completes. It can also be read from the transcript filename (see § Transcripts) or asked from Claude directly.

**Requires** `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (experimental "agent teams" feature). Without it, `SendMessage` is not in the parent's tool set and every `Task` is one-shot. Enable in `~/.claude-agent/settings.json` under `env`, or export in the shell before launching Claude Code.

## Transcripts

Each subagent's full session is persisted independently of the parent transcript:

```
~/.claude/projects/<cwd-hash>/<parent-session-id>/subagents/agent-<agentId>.jsonl
```

One JSONL file per subagent invocation. Each line is one event: the initial user prompt from the parent, the subagent's assistant turns, `tool_use` and `tool_result` blocks, and so on. First record typically has `parentUuid: null` and `isSidechain: true`.

Two properties worth knowing:

### Survives parent compaction and restarts

When the main conversation compacts at ~95% capacity, subagent transcripts are **untouched** — they live in separate files. Resuming the parent session via `claude --resume` brings the transcripts back, so a previously stopped subagent can be reached again with `SendMessage`.

This is the data source for reading prior subagent work during investigation (e.g. inside the `overcome-difficulty` skill): look in `subagents/` next to the parent session file, recent first by mtime.

### Subagent auto-compaction

A long-running subagent has its own context budget and auto-compacts the same way the parent does once it crosses its trigger. The trigger is **not** ~95% of the window: the client derives it as `min(window − round(window × 0.2), window − 13000)` over the window already reduced by the 20 000-token output reserve, which lands near 80% of that reduced figure — about 152k on the 210k pin this fleet uses. The arithmetic, the live symbol names, and the per-model table live in [[autocompact-threshold-policy]]; do not re-derive them here. The compaction event is written into the transcript as a `compact_boundary` row, and a sibling file `agent-acompact-<id>.jsonl` records the compaction metadata (e.g. `preTokens`).

`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` still works in the client (it replaces the 0.2 fraction with `⌊window × pct/100⌋`, still capped at `window − 13000`), but **this fleet does not use it**: `set-context-cap.sh` deletes the key and `lint-settings-base.py` carries it in `DEPRECATED_ENV_KEYS`. To move a child's trigger, pin its window instead — that is exactly what `spawn-specialist.py` does.

Practical implication: do not assume "subagent ran out of context" when work just gets long — auto-compaction handles it. If you really want a fresh state, spawn a new `Task` instead.

### Cleanup

Transcripts are deleted after `cleanupPeriodDays` (default 30) — override in `~/.claude-agent/settings.json`. After deletion, the agent ID is no longer resolvable; `SendMessage` to it will fail.
