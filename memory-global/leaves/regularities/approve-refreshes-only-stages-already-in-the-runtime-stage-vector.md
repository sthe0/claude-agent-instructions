---
name: approve-refreshes-only-stages-already-in-the-runtime-stage-vector
description: Editing a plan in place at PLAN_READY to add stages leaves the runtime stage vector short after approve; only submit-plan rebuilds it
type: reference
schema: regularity/v1
generality: 1
induced_from: [agentctl-stage-vector-staleness]
rule_derived: "lifted from system-knowledge/agentctl-stage-vector-staleness.md (observed 2026-08-19, plan grown 8 to 10 stages, Core issue 126); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# approve-refreshes-only-stages-already-in-the-runtime-stage-vector

## Regularity

When a plan file is edited in place at PLAN_READY so that stages are added or removed, agentctl approve snapshots and hashes the new file but its reconciliation loop (_refresh_caches_from_plan_path) iterates only over stages that already exist in the runtime vector and skips a stage index that is absent, so an added stage is silently dropped. The session then holds an N-stage plan snapshot with a matching accepted_plan_digest and plan_snapshot_hash while state.stages holds the earlier count, nothing warns, and dispatch and verify-final run and attest the truncated plan. From APPROVED no legal edge rebuilds the vector (submit_plan needs PLANNING, revise_plan needs PLAN_READY, reset --force discards the whole session); submit-plan from PLAN_READY rebuilds it through revise_plan.

## Generality

Level 1 — ranges over agentctl sessions whose plan file is edited in place at PLAN_READY so that the stage count changes before approve. It is deliberately not claimed for plan edits that keep the stage count, nor for plans submitted through submit-plan.

## Induced from

- [[agentctl-stage-vector-staleness]] — contributes the observed case behind the statement: a plan grown from 8 to 10 stages in place on 2026-08-19, approved with a vector that stayed at 8.

## Refutation

Growing a plan from 8 to 10 stages in place at PLAN_READY and running agentctl approve without submit-plan, then finding state.stages with 10 stages (and partition --unit accepting stage 9 and 10), would show the statement false.
