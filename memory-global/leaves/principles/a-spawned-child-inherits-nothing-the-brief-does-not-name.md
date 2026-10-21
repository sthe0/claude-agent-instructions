---
name: a-spawned-child-inherits-nothing-the-brief-does-not-name
description: A spawned child process starts with its own session, sandbox, worktree and permission set; whatever authority, context, writable surface or live-data boundary the parent holds is absent in the child unless the brief or the spawn names it.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-06-25-state-gate-needs-acting-session-at-executing-via-toml, 2026-07-02-spawn-sandbox-excludes-declared-stage-material, 2026-07-21-agentctl-dispatch-no-worktree-continuity-dependent-stages, 2026-07-28-spawn-repro-must-not-touch-live-data, 2026-09-23-dispatch-missing-target-project-permissions-forwarding]
created: 2026-10-21
last_verified: 2026-10-21
---

# A spawned child inherits nothing the brief does not name

## Principle

Treat every spawn as a fresh actor: before launching it, list what the child needs that the parent holds implicitly (execution state, the files and material its stage names, the worktree of the prior stage, the target's own permissions) and what it must not touch (live data paths, destructive actions), and pass both explicitly in the brief or the spawn settings; a child that discovers the wall by hitting it spends its budget on denials.

## Generality

Level 2 - a class-of-tasks rule for any delegation to a separate process: the parent's ambient state does not cross the process boundary, in either direction (missing grants, and unbounded reach where the brief names no limit).

## Induced from

- [[2026-06-25-state-gate-needs-acting-session-at-executing-via-toml]] - the spawned developer ran under its own unclassified session, so the parent's execution state authorized nothing and the child was denied every write.
- [[2026-07-02-spawn-sandbox-excludes-declared-stage-material]] - the stage listed a file outside the child's sandbox as material; the child burned its budget probing denials.
- [[2026-07-21-agentctl-dispatch-no-worktree-continuity-dependent-stages]] - a dependent stage forked a fresh worktree and redid the prior stage because the spawn carried no branch context.
- [[2026-07-28-spawn-repro-must-not-touch-live-data]] - a repro brief named no fixture and no forbidden production path, so the child ran against live data.
- [[2026-09-23-dispatch-missing-target-project-permissions-forwarding]] - the child received only the fleet-wide grants, not the target project's own declared build and test permissions.

## Refutation

Refuted by a spawn whose brief and settings omitted its grants, material boundary, prior-stage context or live-data prohibition and which still completed its stage without a denial, a redone predecessor or a touch of live data; or by a failure of a spawned child traced to something the brief did name. Weakened if the five sources are really one engine defect (dispatch does not forward context) rather than a general property of delegation.
