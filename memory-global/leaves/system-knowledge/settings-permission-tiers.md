---
name: settings-permission-tiers
description: Where a tool permission belongs by class — read-only → versioned settings/base.json; code-executing needed by spawned developers → spawn-specialist.py --settings injection; never an exec entry in base.json.
type: reference
created: 2026-07-23
last_verified: 2026-09-26
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

## Grant channel — a third home, for stage-scoped grants

Neither `settings/base.json` (fleet-wide, read-only only) nor a machine-local
settings entry (not durable) is the home for a grant that is neither
fleet-wide nor read-only, but is also not the kind of standing exec allow
`spawn-specialist.py`'s per-kind `KIND_BASELINES` hard-codes. A **third
channel** exists for exactly this: a plan's `[stage.grants]` table
(`scripts/agentctl/README.md` § `[stage.grants]`), validated by
`scripts/agentctl/grants.py` and materialized only into the ONE spawned
child's own `--settings`/`--add-dir` for that ONE stage — never fleet-wide,
never durable beyond the stage. A grant reaches the child through exactly one
of three provenances (declared by the plan author, derived automatically from
the stage's own `verify_command`/output artifacts via `derive_stage_grants`,
or granted at runtime via `resolve-permission` and consumed once at
dispatch) — see [[spawning-specialists]] § File-access scope for the
provenance labels as they appear in a spawned child's own prompt.

**Approval binding.** A derived grant is not free of review just because a
human never typed it: `agentctl approve`/`replan` binds the plan's
`grants_sha256` (the digest of every stage's effective grant set, declared
+ derived) alongside `plan_sha256`, so a plan edit that silently grows the
effective grant set — even purely through derivation, with no `[stage.grants]`
line touched — is a **substantive diff** requiring re-approval, on the same
"an ask's options must span the full set" logic that governs any other scope
widening.

**Never-grantable forms** — `grants.validate_rule`/`validate_add_dir` refuse
these unconditionally, regardless of provenance or wildcard: a bare `*`
Bash command; a compound command (`&&`/`||`/`;`/`|`/`&`/`|&` — one Bash rule
covers exactly one top-level segment); the `claude` program itself; an
agentctl invocation naming a user-authority verb (`AGENTCTL_USER_AUTHORITY_
VERBS` in `lib/widening_targets.py` — these are the **review boundaries**:
verbs only a human-driven `approve`/`resolve`/`plan-review` call may invoke,
never a spawned child's own grant) or a wildcarded agentctl call with no
pinned verb at all; a **settings-channel program**
(`widening_targets.is_settings_channel_program` — anything that could touch
the harness's own settings files); `crontab` (a launch surface); the DSL
family with an unbounded exec primitive (`awk`/`gawk`/`nawk`/`mawk`,
refused unconditionally); `find … -exec/-execdir/-ok/-okdir` (runs an
arbitrary command per matched file); a write-capable program (`tee`, `cp`,
`mv`, `sed`, `dd`, `install`, `rsync`, `ln`, `truncate`, `touch`) named with a
`:*` wildcard, or with no destination argument, or whose destination argument
is a protected G-target; any output redirect (`>`/`>>`/`&>`/`>|`) resolving to
a G-target; and, on the file-tool side, a glob path, a path under `.git`, or a
path that is-or-contains/lies-under a protected root
(`widening_targets.is_live_settings`, `is_agentctl_state_path`,
`is_launch_surface`, `add_dir_is_or_contains_protected_root`,
`add_dir_under_protected_root` — the shared predicates both the Bash-rule and
the file-tool-rule validators call).

**The launch-surface list is narrower than it looks.** `is_launch_surface`
(`lib/widening_targets.py:143`) only covers `~/Library/LaunchAgents`,
`~/Library/LaunchDaemons` and `~/.config/systemd/user`. It does not cover
shell rc files (`~/.bashrc`, `~/.zshrc`, …), `~/.ssh/authorized_keys`, or
`~/.local/bin` (commonly on `PATH`, so a file placed there runs on the
next invocation of its name) — each is a persistent-launch-registration
surface in the same sense the covered three are, but `Edit(//home/the0/
.bashrc)` and a write add_dir onto `~/.local/bin` both validate today.
Separately, a write add_dir onto a project's own `.claude` directory only
denies `.claude/**` *when the add_dir is a parent of it* — `hooks/`,
`agents/` and `skills/` directly under a write add_dir rooted AT
`proj/.claude` itself stay writable, and a read add_dir like `~/.ssh` is
accepted outright (with no complementary deny), including when DR-R
derives one from an absolute reference in the plan. None of this is
mechanized yet; it is a residual the same way the two below are.

**The interpreter-mediated residual.** `validate_rule`'s interpreter
allowlist (`python3`/`bash`/`node`/… ) accepts a rule only when it names a
script-file operand or a safe `-m <module>` (refusing a bare interpreter, an
inline `-c`/`-e`/`-p` eval flag, and a launcher module like `runpy`/`pip`/
`pdb`). That check is a shape check on the **invocation**, not a content
check on the **script**: a validated rule like `Bash(python3
/repo/scripts/foo.py:*)` grants running whatever `foo.py` contains AT
MATERIALIZATION TIME, which may differ from what the reviewer read when the
grant was declared or derived. This residual is bounded by ordinary code
review of the script file itself (it lives in the repo, under the same
review discipline as any other change), not by the grant validator — the
validator's job ends at "this invocation names a script, not inline code."

**The wildcard-admits-extra-flags residual.** `KIND_BASELINES`'
`_READ_ONLY_INSPECTION` rules (`spawn-specialist.py:621-627`) grant every
kind `Bash(find:*)`, `Bash(rg:*)`, `Bash(git log:*)`, `Bash(git diff:*)` as
"read-only" — but `validate_rule` only ever sees the fixed rule text at
grant time, never the actual suffix a `:*` wildcard admits at
materialization time. `find:*` admits `-exec`/`-fprintf`/`-delete`; `rg:*`
admits `--pre <cmd>`; `git log:*`/`git diff:*` admit `--output=<path>`
(e.g. `git log --format=… --output=~/.claude-agent/settings.local.json`).
This is the same shape as the interpreter-mediated residual above — a
validated rule whose *runtime* invocation can differ from what its *text*
implied — and, like that one, is bounded by review of what these baseline
rules actually cover, not by the validator: the "Never-grantable forms"
paragraph's "regardless of provenance or wildcard" claim does not hold for
a pre-approved wildcarded baseline rule of this shape. This does not change
`KIND_BASELINES` itself — pinning these baseline rules to exclude the
dangerous flags is a separate, not-yet-taken fix.

**`settings_drift`.** A spawned child is never granted write access to
`settings*.json`, so `agentctl` hashes every live settings document under
the child's cwd and repo root (`widening_targets.enumerate_live_settings`)
both before and after the child runs, and records a `settings_drift`
finding whenever any of those hashes changed (see `scripts/agentctl/
README.md` § `grant-stats`) — a drifted spawn is a signal that the child
touched settings on disk, not a comparison against what the stage's
declared+derived grant set implies the payload should have been (that
would be a different, not-yet-implemented check).

## See also

- [[instructions-repo-layout]] — the broader repo tree and setup-symlinks path table.
- [[claude-code-settings-env-precedence]] — why the child gets the autocompact knob
  via `--settings` rather than process env (same precedence ladder).
- [[spawning-specialists]] § File-access scope — the grant channel as it appears
  in a spawned child's own generated prompt (provenance lines, write add_dir
  shape, symlink/launch-surface residuals).
