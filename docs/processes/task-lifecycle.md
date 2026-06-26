# The task lifecycle

> How a unit of work travels from arrival to resolution: weight classification → routing → the coordination spine → resolution. The binding rules live in CLAUDE.md; this page is the map.

Every request the system handles is a [task](../concepts/task.md). Its journey is shaped first by **weight**, then driven — for substantive work — by the coordination engine.

## Classify, then route

The first act on any task is to classify its weight, because the class determines the route:

- **Chat** — answered directly in-thread; no plan, no specialist, no recording.
- **Small change** — a tightly bounded edit done in-thread after a brief self-check.
- **Substantive** — everything else, routed through the full coordination cycle.

The concrete thresholds that separate the classes are constants in [config.md](../../config.md); when in doubt between two classes, the heavier one is chosen once and downgraded only if the work visibly fits the lighter class. The classification and routing rule itself is in [CLAUDE.md](../../CLAUDE.md) § Coordination.

## The coordination spine

A substantive task is driven deterministically by the engine, not re-derived as prose each turn. The spine is a fixed sequence of nodes:

```text
classify → plan → approve → dispatch → verify → resolve
```

At each node the engine returns a directive — the next node, which cognitive leaf to run, and whether a gate blocks — while the manager supplies the cognition (the classification judgment, the plan content, the handling of each specialist's return). Two gates on the spine are non-skippable: the **approval gate** holds at plan-ready until the user approves, and the **resolution gate** holds until the user confirms the task is done. The full state machine, its nodes, and the gate mechanism are described in [the coordination engine and its state machine](../architecture/coordination-engine.md).

## Dispatch and verify

Once a plan is approved, the engine dispatches each stage to its actor — the manager itself for in-thread steps, or a spawned specialist for larger ones — and verifies each stage's actual result against the result image the plan declared. A divergence between the two is a *difficulty*, which routes the task into its own sub-process rather than ad-hoc retries (see [difficulty and replan](difficulty-and-replan.md)).

## Resolution

A substantive task is resolved only when the user explicitly confirms it. The closing sequence — a final verification against the done criterion, then recorded confirmation, then the decision of whether to record the experience — is its own process: [resolution and experience](resolution-and-experience.md).

## See also

- [Task](../concepts/task.md) — the weight classes in more detail.
- [The coordination engine and its state machine](../architecture/coordination-engine.md) — the engine that drives the spine.
- [Planning](planning.md) — what happens at the plan node.
