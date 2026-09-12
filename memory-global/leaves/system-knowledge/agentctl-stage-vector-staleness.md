---
name: agentctl-stage-vector-staleness
description: A plan edited in place at PLAN_READY that ADDS or REMOVES stages leaves agentctl's runtime stage vector stale — approve refreshes existing stages only. Re-run submit-plan after any stage-count change, before approving.
type: reference
schema: leaf/v1
created: 2026-08-19
last_verified: 2026-08-19
---

# agentctl stage-vector staleness after a stage-count change

## Difficulty

`agentctl approve` snapshots and hashes the plan file, and `_refresh_caches_from_plan_path`
re-reads it so the gate does not "attest to a plan it never actually executes". But its
reconciliation loop covers only stages that **already exist** in the runtime vector:

```python
for rs in refreshed.stages:
    try:
        cur = state.stage(rs.index)
    except KeyError:
        continue          # a stage ADDED to the plan file is silently dropped
```

So editing the plan in place at PLAN_READY — the normal answer to a thinker `revise`
verdict — refreshes *edited* stages correctly but **drops added ones**. The session then
holds an N-stage plan snapshot with a matching `accepted_plan_digest` and
`plan_snapshot_hash`, while `state.stages` holds the pre-expansion count. Nothing warns.

Observed 2026-08-19 on a plan grown 8 → 10 stages across pre-approval review rounds: it
approved cleanly, and the truncation surfaced only because `partition --unit` named
stages 9 and 10 and was told "stage index N does not exist in the plan". Had partition
not named them, dispatch would have run 8 of 10 stages and `verify-final` would have
attested to a truncated plan. Filed as Core backlog
[#126](https://github.com/sthe0/claude-agent-instructions/issues/126).

## Guidance

**Prevention.** After any plan edit that changes the stage *count* (scope expansion is the
common case — a review round adds a phase), re-run
`agentctl submit-plan --session <id> --plan <path>` **before** `approve`. From PLAN_READY
this takes the supported `revise_plan` edge, which does `state.stages = doc.stages` — a full
rebuild — and preserves `plan_review`, `plan_presentations` and the digest, because
`_still_covers` compares via `changed_parts` and unchanged bytes move nothing. It costs one
increment of `plan_review_rounds` (reset on approve) and re-launches the question
enumeration.

**Repair after the fact.** From APPROVED there is **no legal edge** that rebuilds the
vector: `submit_plan` requires `node=PLANNING`, `revise_plan` requires `node=PLAN_READY`,
and the only in-band alternative, `reset --force`, builds a fresh `SessionState` and
discards the whole session — every plan-review round, order disposal and presentation
receipt with it. The minimal repair is to hand-set the single `node` field back to
`PLAN_READY` in `<state-root>/<session>.json` (back the file up first) and then re-run
`submit-plan`, letting the engine rebuild everything through its own supported path.

**Detection.** `len(state.stages)` vs the plan file's `len(doc['stage'])` — worth a glance
whenever a plan survived several pre-approval review rounds. `agentctl status`'s stage list
reflects the runtime vector, not the file, so a short status listing next to a longer plan
is the visible symptom.

## See also

- [[question-provenance-gate]] — the `premise` gate that guards plan approval; its
  enumeration cross-check re-runs on every `submit-plan`, and on large plans it fails on
  an OS argv limit (Core backlog
  [#127](https://github.com/sthe0/claude-agent-instructions/issues/127)), so a repair
  re-submit usually needs a fresh `question-enumerate-escape`.
- `scripts/agentctl/README.md` — state machine and plan-freeze rules.
