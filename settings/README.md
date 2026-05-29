# Versioned settings base

`base.json` is the **machine-independent, policy-grade** slice of Claude Code's
`~/.claude/settings.json`: the read-only `permissions.allow` allowlist (the
mechanical form of `CLAUDE.md` § Acting without asking carve-out #1) plus the
universal `env` block. It is merged into each machine's live settings by
`scripts/apply-settings.sh` (invoked from `setup-symlinks.sh`).

## What belongs here

- **Yes:** side-effect-free Bash/VCS commands, read-only MCP tools
  (`get_*` / `list_*` / `search_*` / `describe_*`), `WebSearch`, telemetry/limit
  `env` keys — anything safe to grant identically on every machine.
- **No:** anything that writes or mutates state, machine-specific absolute paths
  (`Read`/`Edit`/`Write` under `/home/<user>/…`), hooks, marketplaces, plugins,
  `model`, and ephemeral per-task grants (e.g. one-off `WebFetch` domains). Those
  stay in the machine-local `~/.claude/settings.json` / `settings.local.json`,
  which are not tracked here.

## Guardrail

`git pull` of this base can change what the agent runs **without a prompt**, so
`scripts/lint-settings-base.py` (wired into `verify-all.py`) fails the commit if
any `allow` entry is not read-only. Keep the base side-effect-free.

## Merge semantics

`apply-settings.sh` is idempotent and additive: it unions `permissions.allow`
(base entries first), overlays `env` (local keys win on conflict), and leaves
every other key in the live file untouched. A backup is written to
`<settings>.bak` before each swap.

Not to be confused with `permissions/` — that is **workflow-level** permissions
(higher-level granted actions), a different layer with its own CLI.
