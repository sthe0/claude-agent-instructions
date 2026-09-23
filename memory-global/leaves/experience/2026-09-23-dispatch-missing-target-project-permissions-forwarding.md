---
name: 2026-09-23-dispatch-missing-target-project-permissions-forwarding
description: A spawned spawn:developer stage only ever got the fleet-wide, this-repo-scoped DEVELOPER_SETTINGS_ALLOW Bash grant; the target project's own already-declared .claude/settings.local.json permissions never reached the child's real --settings JSON, so a developer could implement + test a fix correctly but be unable to RUN the target project's own build/test command to attest it.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Сначала починить dispatch (Recommended)"
refs: [2026-07-21-agentctl-dispatch-no-worktree-continuity-dependent-stages]
plan_file: /Users/the0/.claude-agent/plans/the0fun-public-bot.toml
created: 2026-09-23
last_verified: 2026-09-23
---

# agentctl dispatch never forwarded a target project's own settings.local.json permissions to a spawned developer

## Difficulty
agentctl dispatch pins a spawned spawn:developer's cwd to the plan's delivery venue (state.resolve_check_venue(DELIVERY)) but only ever grants it the hardcoded, this-repo-scoped DEVELOPER_SETTINGS_ALLOW list (pytest, verify-all.py, verify-agentctl.py, gen_crutch_registry.py, ...) via spawn-specialist.py's build_child_settings. A target project that has already, deliberately, allow-listed its own build/test commands in its own .claude/settings.local.json gets no benefit from that: DEVELOPER_SETTINGS_ALLOW knows nothing about the target project. A pre-existing --project-permissions flag looked like exactly the needed mechanism but is a red herring: it only feeds permissions_digest, a PROSE summary of a permissions/*.json audit-log file (pattern/granted_at/context shape, managed by permissions-cli.py) embedded into the spawned child's PROMPT — it never reaches the --settings JSON the harness actually gates tool calls against. Confirmed by reading permissions_digest() and permissions-cli.py's digest command from scratch rather than trusting the inherited hypothesis that --project-permissions was already the fix.

## Order & criterion
Add a --project-settings forwarding path, parallel to but structurally distinct from --project-permissions: agentctl/cli.py's cmd_dispatch gains _dispatch_project_settings_path(state), which resolves <delivery-venue>/.claude/settings.local.json off the SAME resolve_check_venue(DELIVERY) call already used for the spawn's cwd, and forwards its absolute path (only if the file exists) through dispatch_stage/build_argv as --project-settings. spawn-specialist.py gains project_settings_permission_rules() (fail-open read of that file's permissions.allow/deny) and build_child_settings(kind, plans_directory, project_settings_file) merges those into a developer child's real --settings grant, kind=="developer" only. Fails open throughout: missing file, unresolved venue, malformed JSON, non-dict shape, or non-string list entries all degrade to forwarding nothing — byte-identical to pre-fix behavior.

**Acceptance check:** 16 new unit/integration tests (project_settings_permission_rules's fail-open branches; build_child_settings's kind-gated merge; build_argv/dispatch_stage threading; cmd_dispatch's 3-way venue-has-file/venue-has-none/no-venue-resolves split) all green; full suite 5972 passed/3 skipped with the same 2 pre-existing unrelated failures as main; verify-all.py all 23 checks pass including verify-semantic-gates after crutch_registry.toml regen; self-review verdict approve-with-nits (2 non-blocking nits); landed to origin/main at b2aa0c3 and confirmed importable/live from the pulled local main checkout.

## Contexts

### 2026-09-23 — initial
- Where it arose: ~/claude-agent-instructions/scripts/agentctl (cli.py, dispatch.py) + scripts/spawn-specialist.py — the agentctl coordination-spine engine, cross-project. Surfaced while executing the trips project's the0fun-public-bot plan (session 0ad1155a-7ef8-4778-9c70-be7a179a0ab2): stage 2's spawned developer did the real work correctly but could not run its own project's pytest to attest it, despite that project's own .claude/settings.local.json already granting the needed Bash pattern.
- Working plan: /Users/the0/.claude-agent/plans/the0fun-public-bot.toml

## Cost
One isolated-worktree session: root-cause investigation (re-reading permissions_digest/permissions-cli.py from scratch to disprove the inherited --project-permissions hypothesis), implementation across 3 files + 2 new test files (16 tests), full suite run (~4 min), verify-all.py, one self-review pass (approve-with-nits, no blocking findings), land-on-main.sh, local-checkout pull-and-verify.

## Self-critique of the agent system
The pre-compaction summary had inherited an unverified premise (--project-permissions is the fix) from earlier investigation; re-derived the actual mechanism from source before designing around it, rather than building the fix on the wrong flag. Also re-applied the land-on-main.sh staged-not-committed discipline and the local-checkout-pull-after-landing lesson learned during the same session's earlier Keychain-auth fix, without re-deriving them from scratch.
