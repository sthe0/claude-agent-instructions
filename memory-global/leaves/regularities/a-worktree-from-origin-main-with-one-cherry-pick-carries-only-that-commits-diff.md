---
name: a-worktree-from-origin-main-with-one-cherry-pick-carries-only-that-commits-diff
description: A worktree at origin/main plus one cherry-pick lands exactly that commit's diff by fast-forward, leaving the feature branch and its dirty tree untouched
type: reference
schema: regularity/v1
generality: 1
induced_from: [landing-changes-core-git-worktree-cherry-pick]
rule_derived: "lifted from landing-changes-core-git-worktree-cherry-pick; no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-worktree-from-origin-main-with-one-cherry-pick-carries-only-that-commits-diff

## Regularity

A push of the local branch to origin main is rejected when local main is stale or the working copy sits on a personal feature branch with unpushed work. A git worktree created at origin/main in a path outside the repo has none of that work, so a git cherry-pick of one sha into it brings only that commit's diff, a push of HEAD:main fast-forwards origin/main by one commit, and the feature branch and its dirty working tree are untouched (the commit also stays on the feature branch, where a later merge treats the duplicate hunk as a no-op). Another session adding commits meanwhile does not affect a cherry-pick addressed by sha. The post-commit message that a push needs user confirmation is an informational hook message, not a block. Observed 2026-07-01 landing a README edit (52dca4f..5af1801).

## Generality

Level 1 — ranges over landing a single commit onto Core git main from a working copy that sits on a feature branch with unrelated unpushed work. It is deliberately not claimed for landing several interdependent commits, nor for repositories with a different trunk-protection setup.

## Induced from

- [[landing-changes-core-git-worktree-cherry-pick]] — contributes the observed case behind the statement: a README edit landed on 2026-07-01 as 52dca4f..5af1801 through a worktree at origin/main.

## Refutation

A cherry-pick of one sha into a worktree at origin/main also brings in other commits or the feature branch's uncommitted changes, or the push of HEAD:main from that worktree is not a fast-forward when origin/main has not moved, or the post-commit reminder actually blocks the git push.
