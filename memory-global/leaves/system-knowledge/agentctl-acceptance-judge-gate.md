---
name: agentctl-acceptance-judge-gate
description: "How agentctl's acceptance-judge gate on record-result --status passed actually works: a haiku model compares --observation against stage.subject.result, verdicts live in state.stage_reviews (not an 'advisor' key)"
type: reference
schema: leaf/v1
created: 2026-08-26
last_verified: 2026-08-26
---

## Difficulty

`agentctl record-result --status passed` returned `ok: true` on one submission and then a
`revise` verdict on the next, with no obvious CLI-level explanation, and searching engine state
for an "advisor" key to find the reasoning found nothing — costing time re-deriving where the
judge's verdict and its stated reason actually live.

## Guidance

Read from `agentctl/cli.py` directly (~lines 4080-4200):

- On `record-result --status passed`, when `requires_observation` is true (an
  `acceptance_review` stage, or **any** stage of a SUBSTANTIVE session), the CLI first checks the
  observation is non-empty and differs from the expected image (`gates._normalize_string`).
- If `gates.stage_review_active(state)`, it then runs
  `advisor.acceptance_judge(observation, stage.subject.result, judge_runner, enabled=True, timeout=advisor._ACCEPTANCE_JUDGE_TIMEOUT_S)`
  — a **haiku-model judge** (shows as `reviewer: "judge:haiku"` in state) that compares the
  submitted `--observation` text against the stage's **declared expected result**
  (`stage.subject.result`, i.e. the plan's per-stage expected-image text).
- The verdict + reason are recorded via `_record_stage_review()` as a `StageReview`
  (`stage_index`, `verdict`, `reviewer`, `note`, `observation_sha256`) appended to
  **`state.stage_reviews`** — a flat list, **not** a top-level `advisor` key.
- The gate then checks `gates.acceptance_review_blockers(state, stage)`, which reads only the
  **latest** `StageReview` for that stage index; `verdict != pass` (and not `override`) blocks
  the pass. An override-cleared pass is recorded separately in `state.judge_bypassed`
  (`JudgeBypass`).
- **Remediation for a genuine `revise` (not "unreachable"): just resubmit `record-result` with an
  observation that actually addresses what the judge's `note` field says was missing.** No
  override/bypass flag is needed — read `state.stage_reviews[-1].note` for that stage index to
  see exactly what the judge found lacking (commonly: the observation covered a bug-fix
  narrative but never restated the stage's *primary* expected outcomes from `subject.result`),
  then write a comprehensive observation covering both.
- `record-result`'s valid flags are only `--session`, `--status {passed,failed}`, `--actual`,
  `--control`, `--observation`, `--code-ref`, `--cost-log` — argparse rejects anything else
  (e.g. a plausible-sounding `--observation-source-note` is not a real flag).

## See also

- [[question-provenance-gate]] — the sibling plan-approval-axis gate (`premise` plugin); this
  leaf covers the resolution-axis acceptance-judge gate instead.
