# Cursor-only subagents

Canonical `*-spawn.md` files live here. They are linked to:

- `~/.cursor/agents/` (user-level, via `cursor/scripts/install-cursor-links.sh`)
- `<project_root>/.cursor/agents/` (per project root, via `cursor/scripts/link-project-cursor-agents.sh` or `setup-local.sh` step 7)

They must not be linked into `~/.claude-agent/agents/` because Claude Code uses a different delegation model for specializations.

Do not keep duplicate regular-file copies under project `.cursor/agents/` — they drift and are not tracked by the project's VCS (see `docs/migrations/2026-05-cursor-namespace.md`).
