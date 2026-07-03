---
name: partition-markers
description: Markers M1-M4 for deciding into how many independently-shippable PRs/tickets a substantive task is partitioned. Apply after a plan exists, before starting implementation. Delivery partition, not the planner's step-level decomposition.
type: reference
created: 2026-05-26
last_verified: 2026-07-03
---

# Partition markers (M1–M4)

A separate question from § Classify task weight in CLAUDE.md. Weight class decides routing (chat / small / substantive). **These markers decide whether a substantive task should ship as one PR or several.**

This is **delivery partition** — how the *approved plan* is cut into shippable units — and is deliberately distinct from the planner's *step-level decomposition* of the task into stages. (That is why the engine stage is named `partition`, not `decompose`.)

Applied after the plan is approved, **before** execution — the `agentctl` engine enforces this: `agentctl partition` sits between the APPROVED and EXECUTING nodes on the spawn route, so a substantive task cannot reach execution without an M1–M4 assessment. You supply the four marker booleans (the cognition below); the engine computes the verdict and renders the section. Adapted from `<arcadia>/ai/artifacts/skills/gena/gena-decompose` — but the framework is repo-agnostic.

## Markers (evaluate top-down)

1. **M1 — Independence.** Can a group of steps / files / contracts be carved out into a PR with **standalone value** — i.e. after that PR merges, the system stays working and the remaining work can continue independently? Without M1, partitioning usually creates churn instead of saving it.
2. **M2 — Heterogeneity.** Does the task mix layers (DB / service / frontend / infra), expertises, or preparatory refactor + new feature in the same change? Each homogeneous slice is easier to review.
3. **M3 — Blocking dependencies.** Is part of the work waiting on an external decision / someone else's PR / another ticket, while another part is unblocked? Split off the unblocked half.
4. **M4 — Rollback risk.** Does the change touch critical surface (migrations, auth, billing, public API) where small steps with separate verification are safer to revert?

Volume alone is not a reason. A 2000-line uniform refactor with no M1 still ships as one PR.

## Verdict (computed by the engine)

`agentctl partition` derives the verdict from the four marker booleans (code: `scripts/agentctl/partition.py`). The truth-table it encodes:

- **recommended** — M1 holds **and** at least one of M2–M4 fires; **or** M3/M4 is flagged severe.
- **possible** — at least one marker fires but not the recommended combination (e.g. weak M1, or M1 alone).
- **not_required** — no marker fires.

You set the markers (cognition); the engine owns the derivation — do not re-derive it by hand.

## Where the verdict goes

`agentctl partition` renders the `## Partition` skeleton (verdict line + which markers fired) into its Directive. Append the cognitive specifics: a 1–2-sentence rationale citing the markers that fired, and — if recommended — a numbered list of sub-PRs (imperative title, 1-line scope boundary, dependency note like `after #1` / `parallel with #2` when material). For an in-conversation plan, surface the verdict before the user-approval gate; the user decides whether to split.

## Materialization & per-unit execution modes

The verdict says *whether* to split; **materialization** records *how each piece is executed and tracked*. A **unit** is an explicit **group of stage indices of the approved plan** — the planner defines the stages, partition only groups and routes them (the boundary stays sharp: no new planning happens here). Units' stage sets must be pairwise disjoint; stages not covered by any unit stay on the default single-PR delivery path.

Each unit carries an org-neutral **mode** — execution context + tracking unit, never a new planning cycle:

- **inline** — executed in the root session, in-thread.
- **spawn** — executed by a spawned specialist inside the root task.
- **subtask** — a separate task with its own coordination record (tracking entry, status, acceptance), which **inherits its plan as the referenced slice of the approved root plan** — no re-plan, no re-approval (approval was given at the root). If the slice diverges during execution, that is a difficulty → `replan` at the **root** plan, never an independent re-plan inside the subtask.

> **Honesty note:** `subtask` records a mode + tracking intent only — its separate-session execution with inherited approval is **not yet mechanized by the engine**. Do not assume engine enforcement. It is also distinct from `push-subplan`, which deliberately runs a *fresh* plan cycle with its **own** approval gate for service sub-plans.

**Spawn vs subtask** — the boundary is *what gets separated*:

| | `spawn` | `subtask` |
|---|---|---|
| Separates | the **actor** (a specialist process) | the **coordination record** |
| Tracking unit | root task's — no new entry | own entry, status, publications |
| Acceptance | covered by the root's resolution gate | own per-unit resolution confirmation |
| Lifetime | bounded by the session | persists across sessions |

Choose `subtask` when at least one holds: different owner/executor (person, machine, time window); temporal decoupling — the unit is blocked externally (M3) or deliberately deferred beyond this session; stakeholders need a separately visible/searchable work item; the user wants to accept this unit separately. Otherwise `spawn`. A subtask has real tracking cost (separate status, publications, resolution) — don't pay it for actor separation alone.

**Cross-unit dependencies are allowed** — a `depends_on` edge between stages of different units imposes a delivery **order** (rendered "after unit N"), it is not a rejection: a dependent unit is simply never presented as independently shippable.

The generic **`ref`** field holds whatever the environment's materialization assigns to a subtask — a tracker key, an issue URL, a child session id. Core stays tracker-agnostic; in a tracker session the tracker plugin observes the `partition` / `partition_units` events and nudges (never gates): propose a delivery structure when the verdict is `recommended`, create a subticket for a `subtask` unit without a `ref` (re-record the unit with the key as `ref` to silence the nudge). The decision always stays with the user.

**CLI surface:** `agentctl partition --unit '<mode>|<stages csv>|<title>[|<ref>]'` (repeatable) records units with the verdict; `agentctl partition-units` (same `--unit` syntax) records or replaces them **after** the verdict is surfaced — allowed only at PARTITIONED or EXECUTING, since the user's structure decision arrives once they have seen the verdict. Re-recording at EXECUTING replaces the list without validating against already-PASSED stages (documented limitation).

## See also

- `~/.claude-agent/CLAUDE.md` § Classify task weight — chat / small / substantive routing (orthogonal axis).
- `<arcadia>/ai/artifacts/skills/gena/gena-decompose/SKILL.md` — the upstream skill this is adapted from.
