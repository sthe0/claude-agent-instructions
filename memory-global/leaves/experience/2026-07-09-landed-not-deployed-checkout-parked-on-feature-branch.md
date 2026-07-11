---
name: 2026-07-09-landed-not-deployed-checkout-parked-on-feature-branch
description: A fix committed to origin/main (hook-ask-defer-timer warn→block, f553e37, 20 tests green) was NOT live because the settings.json hook paths point at ONE checkout of the instructions repo, and that primary checkout was parked on a stale feature branch (26 commits behind main). So 34/36 live hooks silently ran old code although the fix was "landed". "landed ≠ deployed": committing to main does not deploy a hook whose deployed checkout serves a different branch. Also surfaced a second checkout (a separate worktree) pinning 2 hooks to yet another tree — hook code was non-homogeneous across checkouts. Fix: switch the primary checkout back to main (detach the other worktree first to free the branch name), verify runtime (re-run the hook's tests on the DEPLOYED copy), then collapse the redundant checkout so all hooks resolve to one main tree.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Да — решено"
refs: [scripts/hook-ask-defer-timer.py]
created: 2026-07-09
last_verified: 2026-07-11
---

# landed ≠ deployed: a hook fix on main is dead code while the deployed checkout is parked on a feature branch

## Difficulty
Desired: a behavioral fix committed and tested on `origin/main` takes effect on the next turn, because the live hooks run it. Actual: `settings.json` hook commands are absolute paths into a **single** working checkout of the instructions repo, and that checkout was sitting on a feature branch 26 commits behind main — so the deployed hook code was the old branch's version even though the fix was merged. Static verification (fix present on main, `git show origin/main:...` correct, 20/20 tests green **in the main worktree**) all passed while the running behavior was unchanged. A second checkout (a separate worktree) pinned 2 of the hooks to yet another tree, so hook code was not even homogeneous across paths.

## Order & criterion
Deploy a warn→block upgrade to a Stop hook so a promised-but-not-armed deferred ask is caught. Done criterion is a **runtime** property ("the live hook now blocks"), not "the commit is on main" — so the check must run against the *deployed* checkout on the branch it actually serves, not against whatever branch a fresh clone would show.

## Contexts

### The instructions repo is deployed as one checkout referenced by absolute path
`settings.json` hooks are `/abs/path/to/checkout/scripts/hook-*.py`. Whatever branch that checkout has checked out IS the deployed code. Merging to main deploys nothing until that checkout advances to main. This is the git-checkout twin of "posted ≠ published": the artifact exists at the source of truth but the serving surface points elsewhere.

### Why the primary was parked on a feature branch
Feature/bench work had been done *in the primary checkout itself* rather than in a throwaway linked worktree, leaving the primary on that branch after the session ended. Preventive rule: do instruction-repo feature/bench work in a **linked worktree**, keep the primary checkout tracking main, so the live hooks never drift. When multiple checkouts exist, their hook code must be homogeneous — verify with a grep of `settings.json` for stray non-primary paths.

**Mechanized (commit 21837ea).** Rather than a prose reminder or a new parallel hook, the two deterministically-decidable axes were folded into the existing `scripts/hook-instructions-refresh-due.py` (which already did the daily behind-check): it now also emits an `[instructions-deploy]` warning when the Core checkout's HEAD is off `main` (with the correct `git -C <root> switch main` remedy — the daily "pull" nudge could not give it) and when `settings.json` hook commands span >1 checkout root. Extend-over-stack (the thinker plan-review caught that a separate hook would duplicate the behind-check); the "behind" axis was already covered, so only the branch-mismatch and multi-root axes were added.

### The verification that would have caught it
Re-run the changed hook's test suite **against the file the deployed path resolves to** (`realpath` the settings.json command, run its tests from there), and `git -C <that checkout> rev-parse --abbrev-ref HEAD` to confirm the branch. "Tests green in *a* worktree" is not "tests green on the *deployed* copy". Kin to the rule that a graph/pipeline refactor needs a live run in its DoD because static checks miss runtime module-loading.

### Restoring without losing uncommitted work
The same branch can't be checked out in two worktrees. To move the primary back to main while another worktree held main, detach that worktree's HEAD first (`git switch --detach`) to free the branch name, commit/preserve any uncommitted leaves on their branch, then `git switch main` in the primary. Then repoint the second checkout's pinned hooks to the primary and remove the redundant worktree so every hook resolves to one main tree.

## Common core & variations
Core: **a fix on the source-of-truth branch is inert until the surface that serves it advances to that branch** — verify the serving surface's branch/version, not just that the commit landed. Variations already recorded: "posted ≠ published" (a PR pushed to a personal branch is not merged); baked-image runtime axis (code loaded by name from an external artifact needs a live run). This is the same shape for a hook/config deployed via an absolute-path checkout.

## Cost
Diagnosis + restore + consolidation ≈ one session's tail, in-thread (0 spawns). Would-be rediscovery on the next "my hook edit isn't taking effect" is high (silent — no error, tests green), which is what clears the recording bar.
