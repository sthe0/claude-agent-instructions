---
name: a-spawned-child-inherits-nothing-the-brief-does-not-name
description: A spawned child process starts with its own session, sandbox, worktree and permission set; whatever authority, context, writable surface or live-data boundary the parent holds is absent in the child unless the brief or the spawn names it.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-process-boundary-carries-only-what-is-passed-across-it]
created: 2026-10-21
last_verified: 2026-10-21
---

# A spawned child inherits nothing the brief does not name

## Principle

Because a process boundary carries only what is passed across it ([[a-process-boundary-carries-only-what-is-passed-across-it]]), for the goal of spawned actors that spend their budget on their task, treat every spawn as a fresh actor: before launching it, list what the child needs that the parent holds implicitly (execution state, the files and material its stage names, the worktree of the prior stage, the target's own permissions) and what it must not touch (live data paths, destructive actions), and pass both explicitly in the brief or the spawn settings; a child that discovers the wall by hitting it spends its budget on denials.

## Goal class

Spawned actors that spend their budget on their task rather than on discovering missing state, files, worktrees or permissions, and that never touch what they must not.

## Generality

Level 2 - a class-of-tasks rule for any delegation to a separate process: the parent's ambient state does not cross the process boundary, in either direction (missing grants, and unbounded reach where the brief names no limit).

## Grounded in

- [[a-process-boundary-carries-only-what-is-passed-across-it]] — A spawned process starts with nothing the parent holds implicitly — execution state, the files its stage names, the prior stage's worktree, the target's permissions — except what the spawn passes explicitly.

## Refutation

The principle fails its goal if children launched with both lists passed explicitly still hit missing state or permissions, or still touch what they were told not to.
