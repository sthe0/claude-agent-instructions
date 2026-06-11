---
name: harness-read-dedup
description: Difficulty it removes — you are tempted to build a hook to dedup re-Reads, or you burn tokens re-reading an unchanged file. Fact — the Claude Code harness already returns a "Wasted call" stub for a re-Read of an unchanged file (whether first loaded via Read or via a system-reminder), so no custom hook is needed; grounds the no-rereads rule in CLAUDE.md.
type: reference
---

# Harness built-in Read dedup

The Claude Code harness tracks file content shown to the model and short-circuits redundant Read calls. Verified live 2026-05-27 in session `eae4ea0f-...`: an explicit `Read` of `~/.claude/projects/.../memory/MEMORY.md` returned

> `Wasted call — file unchanged since your last Read. Refer to that earlier tool_result instead.`

The same file's content had been delivered earlier in the session via the codebase-and-user-instructions system reminder, not via an explicit Read tool call. The dedup still fired — so the harness counts **any prior surfacing of the content**, not only previous explicit Reads.

## Mechanism (inferred)

- Per-session file history under `~/.claude/file-history/<session-id>/<hash>@v<N>` — versions are written as the harness sees content via Read / Write / Edit or other surface points.
- On a Read tool call the harness compares the on-disk content against the latest in the history for the path. If identical → short-circuit with the "Wasted call" stub. If different → return the new content (with diff prefix) and add a new version.

## What this means for the no-rereads rule in CLAUDE.md

`CLAUDE.md` § Using your tools already states: *"Do NOT re-read a file you just edited to verify — Edit/Write would have errored if the change failed, and the harness tracks file state for you."*

The harness's built-in Read dedup **is** the mechanism behind that promise. The agent should not assume it needs a custom PreToolUse hook to enforce no-rereads — the protection is already there.

## When a custom hook might still help

The built-in does **not** catch every wasteful Read pattern:

- Reading a different file but where the relevant content is a strict subset of an already-loaded file.
- Reading the same file across two distinct sessions (separate session histories).
- Reading after an external (non-tool) change to a file — the harness sees a content change, even if the bytes the agent cares about are unchanged.

For now (2026-05-27) these are not observed at meaningful frequency. If they show up in future cost analysis, add a PreToolUse hook then — not before. See [token-economy-plan.md](../token-economy-plan.md) item 1.

> verified by: live tool call observation in `~/.claude/projects/-home-the0-arcadia-robot-deepagent/eae4ea0f-9c8a-40ba-9aea-3df06793cc7b.jsonl`, 2026-05-27.
