---
name: redispatch-continuity-check
description: Before re-dispatching a stage that was dispatched before, check the repo's existing .claude/worktrees/* and run the stage's verify_command against them — agentctl dispatch forks a fresh worktree for an independent stage every time, so finished work is silently re-derived from zero.
created: 2026-08-28
last_verified: 2026-08-28
schema: leaf/v1
type: reference
---

## Difficulty

`agentctl dispatch` tracks a stage's **status** but not its **artifact** — where the
work physically lives. Continuity is implemented only for the *inter-stage* case:
`agentctl/cli.py::_continuation_worktree` returns `None` unless the stage has a
`depends_on` entry naming a prior SPAWN stage, and it short-circuits on that guard
*before* ever consulting `state.delivery_worktree`. An **independent** stage
(empty `depends_on`) therefore forks a fresh worktree on **every** dispatch.

Re-dispatch is the normal consequence of a permission block or a verify failure, so
this is not an edge case. Observed 2026-08-28: four successive dispatches of one
stage each forked a different worktree, never saw the completed-and-code-reviewed
deliverable sitting in a sibling worktree, and re-derived it from scratch —
~$25 and ~6 active hours on a deliverable that was already done and whose
`verify_command` already passed. Nothing noticed until the effort-divergence
trigger fired on the absolute replan count, four spawns too late.

## Guidance

**Before any re-dispatch of a stage that has been dispatched before:**

1. `ls <repo_root>/.claude/worktrees/` and `git log --oneline -3` in each candidate —
   a prior spawn's committed work lives on a branch nothing told the next spawn about.
2. Run the stage's `verify_command` **directly** against each candidate worktree
   (read-only, no spawn cost). It may already pass.
3. If it passes, close the stage with `agentctl code-review` (a cheap real
   `code-reviewer` spawn, ~$0.75, beats a $2+ developer re-derivation) followed by
   `agentctl record-result --status passed --code-ref <sha>` — do not spawn a developer.

Two mechanical facts that turn a re-dispatch into a *guaranteed* dead end:

- **`agentctl resolve-permission --decision granted` is cosmetic.** `cmd_resolve_permission`
  clears engine state and returns a continuation string via
  `continuations.permission_granted()`; it never writes to any permissions file and
  never touches `spawn-specialist.py`'s static `DEVELOPER_SETTINGS_ALLOW` list. A
  permission block is cleared only by editing that list. Granting and re-dispatching
  reproduces the identical block, every time.
- **Claude Code Bash allow-patterns match the literal command string, not a resolved
  path.** `Bash(python3 samples/judge-latency/sample_x.py:*)` does **not** match
  `python3 sample_x.py` issued from inside that directory. Cover the bare-filename
  form too, or pin the invocation's cwd.

**Also: a plan's prose is not evidence about the plan's own fields.** The same plan's
`goal` field asserted that `[meta] delivery_worktree` had been added; `load_plan()`
returned `None`. Read a declared field back through the loader — never trust the
prose that claims it was written. (Instance of [[doubt-own-snapshot]] applied to an
artifact you authored yourself.)

## See also

- [[effort-divergence-trigger]] — what eventually caught this, and why an absolute
  replan count is a late signal for a "the work is already done" overrun.
- [[spawning-specialists]] — spawn mechanics, including `--continue-worktree`, which
  is the flag `dispatch` declines to thread for an independent stage.
- [[doubt-own-snapshot]] — the general form of the plan-prose-vs-loader-truth trap.
