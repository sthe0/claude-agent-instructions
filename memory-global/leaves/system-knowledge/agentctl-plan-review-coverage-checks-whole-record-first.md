---
name: agentctl-plan-review-coverage-checks-whole-record-first
description: agentctl's plan_review_blockers_coverage gate checks the single whole-plan state.plan_review record FIRST, unconditionally — a stage-scoped override/pass recorded via --scope stage:N is structurally unreachable until the whole record's path+digest binds to the live plan, so re-recording at stage scope alone never clears the gate when the whole record is stale.
type: reference
created: 2026-10-06
last_verified: 2026-10-06
---

## Difficulty

`gates.py::_plan_review_blockers_coverage(state, target_plan, doc)` — the function
`plan_review_blockers` routes to whenever `state.plan_stage_reviews` is non-empty — checks
the single whole-plan record `state.plan_review` **first, unconditionally**, before it ever
looks at `state.plan_stage_reviews[scope]`:

```python
whole = state.plan_review
if whole is None:
    return ["no thinker review recorded ..."]
if whole.plan_path != target_plan and not _binds_across_path_change(whole, target_plan):
    return [_stale_path_blocker(whole.plan_path, target_plan)]
...
blockers = _plan_review_verdict_blockers(whole, state=state, doc=doc)
if blockers:
    return blockers
if not has_pair_records_for(state, target_plan):
    for index in sorted(moved_stages):
        blockers = _stage_route_gaps(state, target_plan, doc, index)   # <-- only reached here
        ...
```

`_binds_across_path_change` checks **byte identity only** (`_file_sha256(target_plan) ==
pr.plan_sha256`), never path-name tricks. So if `state.plan_review` (the whole record) is
still bound to an OLDER plan revision — e.g. left over from an earlier round-release
override cycle, recorded with `scope=""` — and you then record a fresh `plan-review
--scope stage:N` (writing only into `state.plan_stage_reviews["stage:N"]`, per
`cmd_plan_review`'s `if scope: state.plan_stage_reviews[scope] = review else:
state.plan_review = review` branch), that stage-scoped record is **never consulted**: the
stale-whole-path blocker fires and returns immediately, before `_stage_route_gaps` is ever
called. `plan-review` and `replan` both report the identical "stale whole-plan review"
blocker no matter how many times you re-record at stage scope — because the stage-scoped
record was never the thing being checked.

## Guidance

To clear the coverage gate for an edited stage, the **whole-plan** record must itself bind
(path + byte digest) to the live corrected plan. Two ways:

1. Run the whole-plan review/override **without `--scope`** (omit it entirely) — this
   writes into `state.plan_review` directly and re-baselines `reviewed_meta_digest` /
   `reviewed_stage_keys` from the plan being reviewed, so `changed_parts` reports nothing
   moved against that fresh baseline.
2. If a genuine whole-plan review already exists and is current, only *then* does a
   `--scope stage:N` record get consulted via `_stage_route_gaps` for the specific
   stage(s) `changed_parts` flagged as moved.

**Diagnostic signal:** `plan-review` / `replan` keep returning the SAME stale-path blocker
across repeated stage-scoped override attempts. Before re-recording at stage scope again,
dump `state.plan_review` (whole) from the raw state JSON
(`~/.claude-agent/agentctl/state/<session>.json`) and check its `plan_path`/`plan_sha256`
against the target plan's own sha256 — if they don't match, the fix is a whole-scope
review, not another stage-scoped one.

**After clearing `plan_review_blockers`, `replan` can still hit a second, separate gate** —
`plan_approval` premises: a stale per-stage `order-list` coverage disposition (content
digest changed under an order element mapped to that stage — re-confirm via `agentctl
order-dispose --id <R-id> --as covered --stage <n> --plan <corrected-plan>`), and a
raised customer-question candidate. *Historical note:* the standalone `question-enumerate`
cross-check and `question-enumerate-escape` this paragraph used to describe are retired
(both exit 2); customer questions now arrive with each review act as `qrev-` candidates,
and a legacy `qenum-*` candidate still needs `question-candidate-dispose
--as recorded|dismissed` before `replan` proceeds.

## See also

- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the sibling
  false-fail family for `verify_command`/`final_check`, not `plan_review`.
- [[2026-08-27-self-attested-identity-field-needs-declared-principal-check]] — the
  `--verdict override` reviewer-identity binding this leaf's fix also relies on.
