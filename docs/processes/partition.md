# Partition

> How an approved plan is cut into independently-shippable units of delivery, distinct from how the planner decomposed it into stages. The marker criteria live in the partition-markers leaf; this page describes the axis and where it sits.

Decomposition and delivery are two different axes. The planner splits a task into ordered stages — the unit of *execution*. Partition decides whether those stages ship as one pull request or several — the unit of *delivery*. A plan can have six stages and still ship as one PR, or two stages that each warrant their own.

## A separate axis from decomposition

Weight classification decides routing; the planner's stages decide the order of work; partition decides the shape of delivery. Because it is a property of the approved plan, partition is assessed **after** plan approval and **before** execution begins — it never reopens the decomposition, only groups it for shipping.

## The markers M1–M4

The partition verdict is computed from four markers, M1 through M4, each capturing a reason a plan might need to be split into separate independently-reviewable, independently-revertible deliverables (for example: divergent blast radius, mixed reversibility, or independent review audiences). The manager evaluates each marker — that is the cognition — and the criteria for each, along with the severity escalations, are defined in [the partition-markers leaf](../../memory-global/leaves/partition-markers.md). This page does not restate them.

## Machine-gated

Partition is enforced, not advisory. The coordination engine gates execution on the marker assessment: the task cannot advance from approved to executing until the markers have been evaluated and a verdict computed. The engine and its execution node are described in [the coordination engine and its state machine](../architecture/coordination-engine.md).

## See also

- [The partition-markers leaf](../../memory-global/leaves/partition-markers.md) — the M1–M4 criteria and severity escalations.
- [Planning](planning.md) — the decomposition axis partition is distinct from.
- [The task lifecycle](task-lifecycle.md) — where partition sits between approval and execution.
