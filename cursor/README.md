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
- Managed Cursor hooks merged into `~/.cursor/hooks.json` (via `scripts/install-cursor-hooks.py`; registry: `scripts/hooks/desired.json`)

## Native hooks (Cursor host)

Claude hook **policy** stays in `scripts/hook-*.py`. Cursor wiring is generated from the shared registry:

| Piece | Path |
|---|---|
| Registry | `scripts/hooks/desired.json` |
| Host adapter | `scripts/hook-cursor-adapt.py` |
| Installer | `scripts/install-cursor-hooks.py` |
| Session context (`sessionStart`) | `scripts/hook-cursor-memory-context.py` |
| Parity verifier | `scripts/verify-cursor-hook-registry.py` |
| Readiness doctor | `cursor/scripts/cursor-doctor.sh` |

Install after tests pass: `python3 scripts/install-cursor-hooks.py`. Live consumer smoke: `scripts/smoke-cursor-memory-context.py` (requires `agent` CLI + API key).

**Residual gaps** (explicit registry skips — not bugs): no native Skill tool, no native auto-memory extractor, `stop` is bounded follow-up not hard-block, `beforeSubmitPrompt` cannot inject context (sessionStart substitute). Keep Third-party imports disabled.

Installers:

- `cursor/scripts/install-cursor-links.sh` — user-level `~/.cursor/*` (also runs `apply-cursor-config.sh`)
- `cursor/scripts/apply-cursor-config.sh` — merge CLI policy base + permissions symlink
- `cursor/scripts/link-project-cursor-agents.sh` — per-project `<project_root>/.cursor/agents/*`
- `cursor/scripts/migrate-cursor-namespace.sh` — global + optional `--all-configured-roots`
- `cursor/scripts/cursor-doctor.sh` — read-only Cursor host readiness (symlinks, CLI, agentctl); also run from `verify-instructions-sync.sh`
