---
name: a-name-from-position-or-an-empty-variable-designates-a-foreign-object
description: A name for the object of an action that is built from position (top of a stack, current branch) or from a variable that can be empty resolves, under shared use, to an object belonging to someone else or to a parent of the intended one.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat, 2026-07-02-periodic-instruction-refresh-offer-not-silent-pull, 2026-06-29-resume-paused-task-isolated-worktree-pinned-sha, 2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var]
created: 2026-10-27
last_verified: 2026-10-27
---
# A name for the object of an action that is built from position (top of a stack, current branch) or from a variable that can be empty resolves, under shared use, to an object belonging to someone else or to a parent of the intended one

## Regularity

A name for the object of a mutating action that is built from position — the top of a stash stack, the currently checked-out branch, the working tree the session happens to be in — or interpolated from a variable that can be empty resolves, when the resource is shared, to an object the action was not meant for: another session's stash entry, a branch someone else pushed, the parent directory of the intended path. The action then succeeds on the wrong object, and its success report does not name which object it acted on.

## Generality

Level 2 — ranges over mutating actions on resources others may share. It is not claimed for an action whose object is named by an identity that cannot resolve to anything else (a captured sha, a literal path checked non-empty).

## Refutation

A shared resource on which positional or variable-built names, used over many actions, always resolved to the intended object would show the misdesignation does not occur there.

## Induced from

- [[2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat]] - a guarded stash pop applied a stash left by another task and conflicted unrelated files.
- [[2026-07-02-periodic-instruction-refresh-offer-not-silent-pull]] - a background auto-pull rebased a live session's tree mid-task; the fix was an offer through the agent.
- [[2026-06-29-resume-paused-task-isolated-worktree-pinned-sha]] - resuming in a tree held by a parallel session would have corrupted its work and dragged in a foreign commit.
- [[2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var]] - a cleanup built from an empty variable collapsed to a parent directory and deleted the personal memory.
