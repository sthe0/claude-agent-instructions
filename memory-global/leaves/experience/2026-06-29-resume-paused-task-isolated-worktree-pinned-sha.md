---
name: 2026-06-29-resume-paused-task-isolated-worktree-pinned-sha
description: Sharing one VCS working tree with a live parallel session — isolate your work onto a branch based off the PINNED clean base (trunk / pre-WIP SHA), never commit on the parallel session's feature branch. Prevention is an isolated worktree off the clean SHA (git). Recovery when you already committed on the shared branch and stacked onto the parallel session's open PR: cherry-pick your commit onto a fresh trunk-based branch → own PR → owner fast-path merge, then force-restore the shared branch to the parallel session's SHA (arc).
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com (user, 2026-06-29)"
refs: [memory-global/leaves/spawning-specialists.md, memory-global/leaves/coordinator-pitfalls.md, memory-global/leaves/experience/2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution.md, memory-global/leaves/experience/2026-06-30-2026-06-30-verify-spawned-developer-commit-scope-shared-tree.md]
created: 2026-06-29
last_verified: 2026-06-30
last_accessed: 2026-06-30
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

### 2026-06-30 — arc-PR entanglement on a shared storage mount, isolate + restore (si-hook-gates-fix Sub-PR 2)
- Where it arose: the shared `~/arcadia_claude_local` arc mount (project `.claude/` storage), checked out to a live parallel session's feature branch `users/the0/claude-agents` (their open PR #14175489, commit a564e1ab). I committed my project SI fix **on that branch**, so `arc pr create` did not open a new PR — it **stacked my commit onto their open PR**, polluting it (and their next merge attempt went `merge_failed`).
- Prevention (the rule, restated for arc): in a shared arc tree, derive your branch off **`arcadia/trunk`** (`arc checkout -b users/<login>/<slug> arcadia/trunk`) BEFORE committing — never commit on the parallel session's feature branch. `arc push -u <branch>` to create a same-named user ref (a branch created off `arcadia/trunk` tracks trunk, so a bare `arc push` aims at trunk and is `Forbidden`).
- Recovery (already entangled): (1) `arc checkout -b <fresh> arcadia/trunk` + `arc cherry-pick <my-sha>` → isolate; (2) `arc push -u <fresh>` → own PR via `arc pr create --publish` → owner fast-path `arc pr merge --now --force <id>` (merged as r20156524); (3) **re-verify the shared branch server head is still your stacked SHA immediately before** force-restoring (`arc log arcadia/<branch>`), then `arc checkout <shared-branch>` + `arc reset --hard <parallel-SHA>` + `arc push --force` → un-pollutes their PR (drops only your commit; their commit preserved → PR returns to `open`).
- Consequence to handle: `arc reset --hard <parallel-SHA>` reverts your now-landed-on-trunk files **in the working tree**, so the composed `.claude/` live hooks go stale until the mount next reaches a trunk-inclusive state. Verify the plan's final_checks against **trunk content** (`arc show arcadia/trunk:<path>`), not the reverted working tree, then restore the tree to the parallel session's clean SHA. The delivered fix is live in this specific mount only once the parallel branch merges/rebases onto trunk — surface that as a coordination note, it is not a task failure.
- Working plan: ~/.claude/plans/si-hook-gates-fix.toml (4 stages; this is the Stage-4 landing of Sub-PR 2). Sub-PR 1 (global Core, git) landed independently to origin/main with no entanglement.

## Cost
2 developer spawns (stages 3-4) + manager verification; ~1 session

## Self-critique of the agent system
My live-demo harness first under-tested (exec -a collapsed the tree to 1 process and a pid-mismatch marker found 0) — caught and fixed to a genuine 3-process tree before trusting the PASS. Lesson: sanity-check that the verification harness actually exercises the claimed shape before reporting green.
