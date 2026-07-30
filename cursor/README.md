# Cursor namespace

Cursor-specific assets are isolated here so they do not leak into Claude Code runtime paths.

## Layout

- `rules/` — global Cursor rule mirror files.
- `agents/` — Cursor-only subagents (not linked into `~/.claude-agent/agents`).
- `scripts/` — Cursor-only helper scripts.
- `config/` — versioned Cursor CLI policy (`cli-base.json`, `permissions.json`); see [`config/README.md`](config/README.md).

## Runtime links

- `cursor/rules/*.mdc` -> `~/.cursor/rules/*.mdc`
- `cursor/agents/*.md` -> `~/.cursor/agents/*.md`
- `cursor/config/permissions.json` -> `~/.cursor/permissions.json` (via apply)
- `cursor/config/cli-base.json` merged into `~/.cursor/cli-config.json` (via apply)

Installers:

- `cursor/scripts/install-cursor-links.sh` — user-level `~/.cursor/*` (also runs `apply-cursor-config.sh`)
- `cursor/scripts/apply-cursor-config.sh` — merge CLI policy base + permissions symlink
- `cursor/scripts/link-project-cursor-agents.sh` — per-project `<project_root>/.cursor/agents/*`
- `cursor/scripts/migrate-cursor-namespace.sh` — global + optional `--all-configured-roots`
