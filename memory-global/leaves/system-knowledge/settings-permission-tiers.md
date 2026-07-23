---
name: settings-permission-tiers
description: Where a tool permission belongs by class — read-only → versioned settings/base.json; code-executing needed by spawned developers → spawn-specialist.py --settings injection; never an exec entry in base.json.
type: reference
created: 2026-07-23
last_verified: 2026-07-23
schema: leaf/v1
---

## Difficulty

When a task needs to grant an agent tool permission (e.g. "let spawned developers
run `python3 -m pytest`"), the obvious move is to add it to `settings/base.json`
next to a lookalike entry (`Bash(python3 -m json.tool:*)`). For a **code-executing**
permission this is wrong and is caught only at final verification: `verify-all` →
`lint-settings-base.py` **FAILS** ("non-read-only entry in base.json"). Rediscovering
the correct home mid-task costs a full replan.

## Guidance

Two versioned sources feed a machine's live `~/.claude-agent/settings.json`
`permissions.allow` (via `apply-settings.sh`, a union merge — base first, then
local-only entries preserved):

1. **`settings/base.json`** — merged into **every** machine on `git pull` **without
   a prompt**. By security invariant it may hold **only side-effect-free (read-only)**
   entries; `scripts/lint-settings-base.py` (a `verify-all` check) fails on anything
   that could mutate state. Read-only classes: `Read(...)`/`WebSearch`/`WebFetch`,
   read-only MCP (`get`/`list`/`search`/`describe`), `Bash(<verb>…)` where verb ∈
   `READONLY_BASH`, read-only `git`/`arc` subcommands, and `python3` only for
   `-c "` or `-m json.tool` (`READONLY_PYTHON3`). **`pytest` is NOT read-only** — the
   `json.tool` "sibling precedent" does **not** license it.

2. **Machine-local** entries in the live settings file — preserved by the union merge
   but **not versioned/shareable**, so no good for a fix other machines must inherit.

A **code-executing** permission scoped to spawned developers has no valid home in
either: not base.json (fails the fleet-wide read-only invariant — you must never let
`git pull` silently grant code-execution fleet-wide), not machine-local (not durable).
The correct structural home is **`scripts/spawn-specialist.py`**, which launches the
developer child as `claude -p --settings '{"env":{…}}' --permission-mode
bypassPermissions`. Inject the exec allow into that `--settings` payload for
`kind == "developer"` (`"permissions": {"allow": ["Bash(python3 -m pytest:*)"]}`):
developer-spawn-scoped, versioned in the repo, never merged fleet-wide. Note
`--permission-mode bypassPermissions` alone does **not** unblock a bare
code-executing command — the Bash command-safety classifier is independent of
permission mode; the allow entry is what lifts it. Verify a permission change to a
spawn end-to-end with a **live** diagnostic spawn (does the child actually run the
command without an approval prompt), not just a static test asserting the entry exists.

## See also

- [[instructions-repo-layout]] — the broader repo tree and setup-symlinks path table.
- [[claude-code-settings-env-precedence]] — why the child gets the autocompact knob
  via `--settings` rather than process env (same precedence ladder).
