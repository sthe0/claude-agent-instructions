---
name: 2026-07-02-seam-stub-tests-never-exercise-default
description: onboard hook's SETUP_LOCAL_BIN default (../../scripts) broke when the storage layout moved (agents/scripts -> agents/common/scripts); every test case stubbed the seam, so 17 green tests coexisted with a hook that failed on every real init
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (2026-07-02, resolution gate click)"
refs: [memory-global/leaves/experience/2026-06-26-guard-coupled-doc-relocation.md]
created: 2026-07-02
last_verified: 2026-07-02
---

# A seam-stubbed test suite never exercises the seam's default — layout moves silently break relative-path defaults

## Difficulty
Env-seam testability (X_BIN overridable defaults) is good design, but when EVERY test case injects the stub, the default value itself has zero coverage. A default that encodes a relative path couples silently to the directory layout; a layout-move commit passes the whole suite while breaking every fresh-machine init. Failure surfaced repeatedly at user-visible launch time (claude-personal onboard failing on each start, re-running init every launch because the plugin symlink step never completed).

## Order & criterion
Root-cause the failing path by reading how the default is DERIVED ($_HOOK_DIR/..) rather than where it points; fix the default; add one test case that runs the artifact WITHOUT the stub (env -u SEAM, dry-run mode) and asserts the printed/derived default resolves against the real tree; land via an isolated parallel mount because a live session held the shared tree's branch.

**Acceptance check:** Suite green including the new default-resolution case; CLAUDE_DRY_RUN run prints an existing path; --needs-init returns already-initialized; PR merged to trunk and content confirmed there

## Contexts

### 2026-07-02 — initial
- Where it arose: junk/the0/agents/common/onboard/10-arc-mount.sh + tests/test-arc-mount-hook.sh; PR 14232164; generalizes to every X_BIN-style seam whose default is a relative path
- Working plan: /home/the0/.claude-agent/plans/fix-task-entry-onboarding-failures.toml

## Cost
~40 min wall-clock, 3 stages in-thread, 1 arc PR, 2 Core commits

## Self-critique of the agent system
The regression shipped in a layout-move commit this agent system authored; a dry-run default-path assertion at that time would have caught it. Also: verify-all.py (pre-commit) does not run the shell layout contract — same class of gap (checker exists, gate never fires), left as recorded follow-up.
