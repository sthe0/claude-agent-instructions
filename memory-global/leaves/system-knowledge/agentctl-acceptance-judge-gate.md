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
- **Remediation for a genuine `revise`: resubmit with an observation addressing the judge's
  `note` — but this does NOT reliably converge.** Observed 2026-08-26 (unify-loop-prevention
  stage 5): 6 successive resubmissions, each concretely addressing the *exact* gap named in the
  previous `note` (verbatim test names, line numbers, exit codes, on-disk symbol existence —
  strictly more specific each round, never a rephrase), were rejected with a **different** stated
  gap each time (goalpost-moving) rather than converging on a stable, satisfiable criterion. The
  single `haiku`-model YES/NO call has no consistency/majority-vote safeguard, so it is not
  reliable on long, technically-dense payloads. Treat 2-3 genuinely-improving resubmissions with
  no convergence as the signal to stop and use the override escape below — not as evidence the
  next rephrase will finally work.
- **A `revise` and a silently-failed judge call are not distinguishable from the blocker text
  alone — check `state.stage_reviews[-1]` before rewriting content.** `advisor.acceptance_judge`
  fails open (`verdict=None`) on timeout/exception/unparseable output; this was observed in
  practice specifically on the two *longest* observations submitted (~1500+ chars) — the failure
  mode disproportionately hits the long, evidence-dense text the gate nominally rewards. On a
  `None` verdict no new `StageReview` is written, so `gates.acceptance_review_blockers` compares
  the current observation's hash against the stale prior review and reports
  `"acceptance judge verdict is 'stale'"` — wording that reads as "just re-judge," not "the judge
  call itself failed." Compare `state.stage_reviews[-1].observation_sha256` against
  `sha256(current --observation)` (or just retry the identical submission once) before spending
  another round rewriting content that was never actually judged.
- **Escape hatch when a genuinely-complete, factually-verified observation still won't clear:**
  `agentctl stage-review --session <sid> --verdict override --reviewer <who> --note <reason>`
  (requires non-empty `--reviewer` + `--note` — "the user's explicit escape reason" per
  `gates.py::acceptance_review_blockers`'s own docstring) binds an `override` verdict to the
  stage's *current* observation bytes; the next `record-result --status passed` then clears via
  `gates._STAGE_REVIEW_OVERRIDE`, and the bypass is recorded visibly in `state.judge_bypassed`
  (`JudgeBypass`) so `verify-final`/`resolve` surface that this particular pass was
  judge-overridden rather than judge-approved. **Requires user sanction before invoking** — an
  internal gate's override names its constraint and is not self-granted; surface the judge's
  actual verdict history and ask, don't self-override silently.
- `record-result`'s valid flags are only `--session`, `--status {passed,failed}`, `--actual`,
  `--control`, `--observation`, `--code-ref`, `--cost-log` — argparse rejects anything else
  (e.g. a plausible-sounding `--observation-source-note` is not a real flag).

## See also

- [[question-provenance-gate]] — the sibling plan-approval-axis gate (`premise` plugin); this
  leaf covers the resolution-axis acceptance-judge gate instead.
