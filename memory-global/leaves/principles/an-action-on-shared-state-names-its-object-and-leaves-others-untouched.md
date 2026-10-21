---
name: an-action-on-shared-state-names-its-object-and-leaves-others-untouched
description: An operation that assumes it owns the state it touches ('whatever stash is on top', 'pull now', 'the variable's directory') damages state that belongs to someone else when the assumption fails.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat, 2026-07-02-periodic-instruction-refresh-offer-not-silent-pull, 2026-06-29-resume-paused-task-isolated-worktree-pinned-sha, 2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var]
created: 2026-10-21
last_verified: 2026-10-21
---

# An action on shared state names its object and leaves others untouched

## Principle

Before a mutating act on a resource other sessions or tasks may share (a stash, a working tree, a branch, a directory built from a variable), name the exact object by identity, guard the conditions under which the name is empty or foreign, and prefer an offer to a user over a silent background mutation; recover by isolating onto a pinned clean base rather than editing in place.

## Generality

Level 2 - a rule for any write to a resource with more than one potential owner.

## Induced from

- [[2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat]] - a guarded stash pop applied a stash left by another task and conflicted unrelated files.
- [[2026-07-02-periodic-instruction-refresh-offer-not-silent-pull]] - a background auto-pull rebased a live session's tree mid-task; the fix was an offer through the agent.
- [[2026-06-29-resume-paused-task-isolated-worktree-pinned-sha]] - resuming in a tree held by a parallel session would have corrupted its work and dragged in a foreign commit.
- [[2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var]] - a cleanup built from an empty variable collapsed to a parent directory and deleted the personal memory.

## Refutation

Refuted by a mutation of shared state that was done by position or ambient default ('the top stash', 'the current directory') and was safe by construction, or by a case where naming the object cost more than the collisions it would prevent.
