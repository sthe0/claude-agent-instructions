---
name: a-process-boundary-carries-only-what-is-passed-across-it
description: A spawned process starts with nothing the parent holds implicitly — execution state, the files its stage names, the prior stage's worktree, the target's permissions — except what the spawn passes explicitly.
type: reference
schema: regularity/v1
generality: 2
induced_from: [2026-06-25-state-gate-needs-acting-session-at-executing-via-toml, 2026-07-02-spawn-sandbox-excludes-declared-stage-material, 2026-07-21-agentctl-dispatch-no-worktree-continuity-dependent-stages, 2026-07-28-spawn-repro-must-not-touch-live-data, 2026-09-23-dispatch-missing-target-project-permissions-forwarding]
created: 2026-10-27
last_verified: 2026-10-27
---
# A spawned process starts with nothing the parent holds implicitly

## Regularity

A process boundary carries only what is passed across it: a spawned child starts without the parent's execution state, without the files and material its stage names unless they are inside its sandbox, without the worktree of the stage before it, without the target project's permissions, and without any knowledge of what it must not touch. What the parent held implicitly is absent on the other side, and the child discovers each absence only by hitting it — a denial, a missing file, an action on live data.

## Generality

Level 2 — ranges over any spawn of a fresh actor — a `claude -p` specialist, a subprocess with its own sandbox, a dispatched stage. It is not claimed for an in-process call that shares the caller's state.

## Refutation

Spawns observed with briefs that named nothing beyond the task text, whose children nonetheless never hit a missing state, file, worktree or permission, would show the boundary carries more than what is passed.

## Induced from

- [[2026-06-25-state-gate-needs-acting-session-at-executing-via-toml]] - the spawned developer ran under its own unclassified session, so the parent's execution state authorized nothing and the child was denied every write.
- [[2026-07-02-spawn-sandbox-excludes-declared-stage-material]] - the stage listed a file outside the child's sandbox as material; the child burned its budget probing denials.
- [[2026-07-21-agentctl-dispatch-no-worktree-continuity-dependent-stages]] - a dependent stage forked a fresh worktree and redid the prior stage because the spawn carried no branch context.
- [[2026-07-28-spawn-repro-must-not-touch-live-data]] - a repro brief named no fixture and no forbidden production path, so the child ran against live data.
- [[2026-09-23-dispatch-missing-target-project-permissions-forwarding]] - the child received only the fleet-wide grants, not the target project's own declared build and test permissions.
