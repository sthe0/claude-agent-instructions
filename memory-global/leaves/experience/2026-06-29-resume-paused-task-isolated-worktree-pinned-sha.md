---
name: 2026-06-29-resume-paused-task-isolated-worktree-pinned-sha
description: Resuming a paused multi-stage task whose git working tree is still occupied by a live parallel session: the safe resume is an isolated git worktree based off the PINNED clean SHA (base before either session's WIP), not off the feature branch tip — the parallel session may have advanced that branch.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com (user, 2026-06-29)"
refs: [memory-global/leaves/spawning-specialists.md, memory-global/leaves/coordinator-pitfalls.md]
created: 2026-06-29
last_verified: 2026-06-29
last_accessed: 2026-06-29
---

# Resume a paused task sharing a git tree with a live parallel session via an isolated worktree off a pinned clean SHA

## Difficulty
A paused task (recursive-kill-spawns, stages 1-2 on branch recursive-kill-spawns-resume's ancestor) had to resume while a parallel Claude session (agentctl-cognition-to-code) still held the SAME git working tree with uncommitted WIP — AND had pushed an unrelated cost-tracking commit (6fbd567) onto the very feature branch (recursive-kill-spawns) the paused task used. Resuming in-place (checkout/stash/commit -a) would corrupt the parallel session's tree; branching off the feature-branch tip would drag in the foreign commit.

## Order & criterion
1) confirm the parallel session is still live (uncommitted WIP + extra claude procs); 2) identify the PINNED clean SHA = the last commit that is purely this task's stages (d42b4bc), NOT the branch tip; 3) git worktree add -b <new-branch> <path> <pinned-SHA> (worktree add never perturbs the shared dirty tree); 4) do all remaining stages in the isolated worktree; 5) verify a process-lifecycle change on the RUNTIME axis (live SIGTERM to a supervised tree -> 0 orphans), not import/test-pass alone; 6) the repo pre-commit verify-all gate demands a scripts/README.md row + executable bit for any new script — fix before commit.

**Acceptance check:** measurable: worktree builds off the pinned SHA with the parallel tree untouched; pytest 694 green + verify-agentctl 0 + live demo 0 orphans; verify-all pre-commit 13/13

## Contexts

### 2026-06-29 — recursive-kill-spawns resume (2026-06-29)
- Where it arose: claude-agent-instructions (git repo) meta-work; any resume of a paused task whose tree is shared with another active agent session
- Working plan: 5-stage TOML plan ~/.claude/plans/recursive-kill-spawns.toml; stages 3-4 spawned as developer agents into the worktree, stage 5 manager runtime verification

## Cost
2 developer spawns (stages 3-4) + manager verification; ~1 session

## Self-critique of the agent system
My live-demo harness first under-tested (exec -a collapsed the tree to 1 process and a pid-mismatch marker found 0) — caught and fixed to a genuine 3-process tree before trusting the PASS. Lesson: sanity-check that the verification harness actually exercises the claimed shape before reporting green.
