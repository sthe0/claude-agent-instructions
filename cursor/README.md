# Cursor namespace

Cursor-specific assets are isolated here so they do not leak into Claude Code runtime paths.

## Layout

- `rules/` — global Cursor rule mirror files.
- `agents/` — Cursor-only subagents (not linked into `~/.claude/agents`).
- `scripts/` — Cursor-only helper scripts.

## Runtime links

- `cursor/rules/*.mdc` -> `~/.cursor/rules/*.mdc`
- `cursor/agents/*.md` -> `~/.cursor/agents/*.md`

Installers:

- `cursor/scripts/install-cursor-links.sh` — user-level `~/.cursor/*`
- `cursor/scripts/link-project-cursor-agents.sh` — per-mount `robot/deepagent/.cursor/agents/*`
- `cursor/scripts/migrate-cursor-namespace.sh` — global + optional `--all-deepagent-mounts`
