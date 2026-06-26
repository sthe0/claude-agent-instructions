# Planning

> How a substantive task acquires a plan before any production code is written: the planner specialist, the activity ontology a plan must cover, and the approval gate. The binding detail lives in the planner skill and the ontology leaf; this page describes when and why planning runs.

A substantive task does not proceed to execution on intuition. It first acquires an explicit, approved plan — the artifact the coordination spine then dispatches stage by stage.

## When a plan is required

Chat and small-change tasks need no plan; they are handled in-thread. A **substantive** task — multi-file, architectural, externally-effecting, ambiguous, or long-running — always routes through planning first. The decision is the weight classification described in [the task lifecycle](task-lifecycle.md).

## The planner specialist

Planning is the job of the planner specialization, invoked inline for a short refinement or spawned as a separate process for a larger, multi-stage plan. The planner decomposes the task into ordered stages with dependencies, risks, and a measurable done criterion per stage. Its full contract — invocation modes, inputs, and return markers — is in [the planner skill](../../skills/specializations/planner/SKILL.md).

## What a substantive plan must cover

A substantive plan is not a free-form to-do list. It must cover a fixed set of activity elements — order, material and result, control criterion, means, method, conditions and invariants, actor and capability, and a refutable principle — so that every stage states what it acts on, how its result is checked, and who performs it. These elements and their mapping to the engine's typed plan model are defined in [the plan activity ontology](../../memory-global/leaves/plan-activity-ontology.md); this page does not restate them.

## The approval gate

A finished plan is not self-authorizing. The engine holds the task at plan-ready behind a non-skippable **approval gate**: production edits stay hard-denied until the user explicitly approves the plan, and approval is never inferred from silence. Only after approval does the spine advance to dispatch. The gate mechanism is part of [the coordination engine and its state machine](../architecture/coordination-engine.md).

## See also

- [The task lifecycle](task-lifecycle.md) — where planning sits in the end-to-end flow.
- [The plan activity ontology](../../memory-global/leaves/plan-activity-ontology.md) — the elements a plan must cover.
- [Difficulty and replan](difficulty-and-replan.md) — how a plan is revised when a stage diverges.
- [Partition](partition.md) — cutting an approved plan into shippable units.
