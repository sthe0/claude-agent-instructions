---
name: agentctl-approve-stale-runtime-plan
description: agentctl approve/plan-review do NOT rebuild runtime state.stages/final_check from an edited plan; only submit-plan (from PLANNING) and replan (from DIAGNOSING) do. A plan edited between submit-plan and approve silently carries a stale stage list + old final_check into partition/execution/verify-final. Symptom - partition rejects a stage index that exists in the plan.
type: reference
schema: leaf/v1
created: 2026-07-10
last_verified: 2026-07-10
---

# agentctl: `approve` does not resync runtime plan state from an edited plan

## Difficulty

The engine keeps a **runtime** copy of the plan on `SessionState` — `state.stages` and
`state.final_check` — separate from the frozen approve-time **snapshot**
(`plan-approved-<sha>.toml`). Partition validation (`cmd_partition`, cli.py:674 —
`known = {s.index for s in state.stages}`), stage dispatch (`cmd_next_stage`), and final
verification (`cmd_verify_final`) all read the **runtime** copy, not the snapshot.

Only three commands rebuild the runtime copy from the plan file:
`cmd_submit_plan` (cli.py:477, from `PLANNING`), `cmd_replan` (from `DIAGNOSING`), and the
reset/pop-subplan paths. **`cmd_approve` re-snapshots the plan (picks up current bytes) but
does NOT rebuild `state.stages`/`state.final_check`.** `cmd_plan_review` records a verdict
and touches neither.

So this sequence silently diverges: `submit-plan` (captures N stages) → edit the plan file
(add a stage, rewrite a `final_check`) → `plan-review` (records pass, no rebuild) → `approve`
(re-snapshots to the NEW bytes, runtime stays at the OLD N stages). The snapshot hash matches
the live file, yet the runtime stage list is stale.

**Observed 2026-07-10** (`claude-task-opening-dialogue`, session `a2b2e7c7`): plan grew from
9→10 stages and 9→10 final_checks across revisions 9–15, all applied via `plan-review` with no
re-`submit-plan`. Runtime froze at 9 stages (missing stage 0) with the pre-R10–R13
`final_check` (0 `core.quotePath=false` vs 1 in the approved file). Symptom: `agentctl
partition --unit 'spawn|0,...'` rejected with `stage index 0 does not exist in the plan`.
Had it not been caught, execution would have run the wrong stage set and the **old** guards.

## Guidance

- **After ANY edit to the plan file while at `PLAN_READY`, re-run `submit-plan` (resubmit)
  before `approve`** — that is the only in-band way to pull the edit into the runtime copy.
  Recording a `plan-review` is not enough.
- **Diagnosing a stale runtime:** `state.stages` indices and `len(state.final_check)` vs the
  live plan (load via `plan.py`, not raw `tomllib` — raw `tomllib` does not surface the stage
  array under the `stages` key). Compare `final_check[i]["command"]` to the live plan.
- **Recovery when already past `APPROVED`** (no `APPROVED→submit` edge exists): `reset --force`
  + re-drive the whole spine (`classify → plan → submit-plan → plan-review → approve →
  partition`) over the **byte-identical** approved plan. Every re-recorded event stays faithful
  only if the content is unchanged (assert the sha256 first) and the review/approval were
  genuinely given for that exact content. Never hand-edit the state JSON — it is the engine's
  control surface.
- **Latent Core defect (fix candidate, route via self-improvement):** `cmd_approve` should
  either rebuild the runtime copy from the re-snapshotted plan, or REFUSE to approve when
  `state.stages`/`state.final_check` diverge from the live plan file (a `verify-plan-file`
  parity check at the approve gate). Prefer the refusal — it surfaces the divergence loudly
  instead of silently resyncing behind the user's back. This is a public-repo Core change and
  rides its own authority check + plan-approval spine.

## See also

- `~/claude-agent-instructions/scripts/agentctl/cli.py` — `cmd_submit_plan` (477),
  `cmd_approve` (586), `cmd_partition` (667/674), `cmd_replan` (1304, `state.stages = new.stages`).
- `~/claude-agent-instructions/scripts/agentctl/machine.py` — transition table (no
  `APPROVED→PLAN_READY` edge; `revise_plan` is `PLAN_READY→PLAN_READY` only).
