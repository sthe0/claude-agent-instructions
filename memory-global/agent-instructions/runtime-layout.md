# Runtime layout (canonical paths)

Where to find components after `scripts/setup-symlinks.sh`. Full tree and reconciliation duty — [file-structure-contract.md](file-structure-contract.md).

## Git (global, same everywhere)

| What | Where |
|-----|-----|
| Instructions repository | `~/claude-agent-instructions/` |
| Policy | `CLAUDE.md` → `~/.claude/CLAUDE.md` |
| Global agents | `agents/*.md` → `~/.claude/agents/<name>.md` |
| Global memory | `memory-global/` → `~/.claude/memory-global/` |
| Cursor rule | `cursor-rules/claude-code-sync.mdc` → `~/.cursor/rules/` |

## Runtime (same names, source may differ)

| What | Where to look |
|-----|----------------|
| All subagents (global + optional local) | `~/.claude/agents/` — by `name` in frontmatter |
| Skills | `~/.claude/skills/` |
| Local domain memory | `~/.claude/memory/INDEX.md` |
| Global memory | `~/.claude/memory-global/INDEX.md` |
| Global scripts (git) | `~/claude-agent-instructions/scripts/` |
| Local scripts (arc) | `~/.claude/scripts-local/` |

Local configuration (memory, extra agents, arc sync) — on the machine, not in instructions git. Symlinks: `~/claude-agent-instructions/scripts/setup-symlinks.sh`. Runbook: `~/.claude/memory/INDEX.md`.

## Metadata

| Field | Value |
|------|----------|
| `last_verified` | 2026-05-21 |
| `staleness_triggers` | change to `setup-symlinks.sh` |
| `revalidate` | `~/claude-agent-instructions/scripts/verify-layout-contract.sh` |
