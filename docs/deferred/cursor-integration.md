# Claude Code and Cursor: single instruction source (deferred)

> **Deferred snapshot.** This document predates the agent-system refactor (manager / memory agents removed; `overcome-difficulty` and `self-improvement` are skills now; memory rebuilt on native Claude Code auto-memory with `memory-global/MEMORY.md` + `<project>/.claude/agent-memory/`). The Cursor wiring described below is **not** current — keep it here as a reference, rework it as a separate step once the Claude side stabilizes.

## Architecture

```text
~/claude-agent-instructions/     ← git (single source of truth)
├── CLAUDE.md
├── agents/*.md
├── agents-local/*.md            ← gitignored (optional, see agents-local/README.md)
├── cursor-rules/
│   ├── claude-code-sync.mdc     ← global for Cursor
│   └── project-overlay-deepagent.mdc  ← overlay template for robot/deepagent
└── memory-global/INDEX.md

~/.claude/CLAUDE.md              → symlink
~/.claude/agents/<agent>.md      → symlink (per file)
~/.claude/memory/INDEX.md        → symlink
~/.cursor/agents                 → symlink to ~/.claude/agents
~/.cursor/rules/claude-code-sync.mdc → symlink

<project>/.cursor/rules/         ← overlay only (do not duplicate global policy)
<project>/CLAUDE.md              → optional symlink to ~/.claude/CLAUDE.md
```

## Who reads what

| Tool | Global policy | Agents | Memory INDEX |
|------------|---------------------|--------|--------------|
| **Claude Code** | `~/.claude/CLAUDE.md` | `~/.claude/agents/*.md` | `~/.claude/memory/INDEX.md` |
| **Cursor** | `~/.cursor/rules/claude-code-sync.mdc` + same `CLAUDE.md` in project (if symlink) | `~/.cursor/agents` (= `.claude/agents`) | same INDEX |

**Canonical text** for global rules — `CLAUDE.md` in the repo. `claude-code-sync.mdc` mirrors mandatory gates for Cursor (`alwaysApply`) and defers to `CLAUDE.md` on conflict.

## Sync across machines and IDEs

1. **Git:** `pull` before edit, `commit` + `push` after (`instructions-git-sync.md`).
2. **Symlinks:** `scripts/setup-symlinks.sh` after clone/pull on a new machine.
3. **Background:** systemd timer or cron — `pull` every 10 min.
4. **Verify:** `scripts/verify-instructions-sync.sh` — symlinks, no stale copies.

## Edit rules (agent)

| What to change | Where |
|------------|-----|
| Global policy, workflow, manager/self-improvement | `CLAUDE.md` + mirror in `cursor-rules/claude-code-sync.mdc` |
| One agent's role | `agents/<name>.md` |
| Cursor-only (globs, project) | `cursor-rules/project-overlay-*.mdc` |
| deepagent domain only | `~/.claude/memory/deepagent/` (not in instructions git) |

**Forbidden:** full copy of `claude-code-sync.mdc` inside Arcadia project — only **overlay** from `project-overlay-deepagent.mdc`.

## robot/deepagent

- `CLAUDE.md` → `~/.claude/CLAUDE.md` (symlink)
- `.cursor/rules/deepagent-project.mdc` — overlay (permissions, deepagent memory), not a replacement for the global rule
