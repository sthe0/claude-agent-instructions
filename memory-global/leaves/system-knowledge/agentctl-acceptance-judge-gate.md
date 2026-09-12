---
name: agentctl-acceptance-judge-gate
description: "How agentctl's acceptance-judge gate on record-result --status passed actually works: a haiku model compares --observation against stage.subject.result, verdicts live in state.stage_reviews (not an 'advisor' key); the predicted short-observation mitigation is now falsified by a second confirmed occurrence"
type: reference
schema: leaf/v1
created: 2026-08-26
last_verified: 2026-09-03
---

## Difficulty

`agentctl record-result --status passed` returned `ok: true` on one submission and then a
`revise` verdict on the next, with no obvious CLI-level explanation, and searching engine state
for an "advisor" key to find the reasoning found nothing — costing time re-deriving where the
judge's verdict and its stated reason actually live.

## Guidance

Read from `agentctl/cli.py` directly (~lines 4080-4200):

- **The required observation shape** (`cli.OBSERVATION_CONTRACT`, stated at every authoring point —
  `record-result --observation` help, `close --observation` help, and both observation-refusal
  Directives): attest in the present tense what you observed: name the artifact (file, command,
  output) and state what reading it showed. Do not narrate what had been wrong or how it was fixed
  — a defect history is not an observation. Keep it short and targeted (~400-500 chars); a long
  cumulative observation makes the judge both more likely to move the goalposts and more likely to
  time out.
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
  next rephrase will finally work. **Root cause (confirmed 2026-08-26, stage 7: 5 more
  goalpost-moving rounds + 2 more fail-opens on top of stage 5's 6):** the judge's prompt
  (`advisor._PROMPTS["acceptance_observation"]`) does not literally demand exhaustive per-fact
  coverage — it asks only whether the observation is "vague, generic, or a rephrase" — but when a
  stage's `expected_result_image` bundles several distinct facts into one long sentence, the
  cheap model has no memory between calls, so each independent call over-interprets "concrete and
  adequate" as requiring ALL facts covered *in that one observation*, naming a different missing
  subset each time. No single observation short enough to dodge the length-correlated fail-open
  (next bullet) can also cover every fact — a genuine moving-target deadlock, not a resubmission-
  quality problem. Predicted mitigation: keep `--observation` short (~400-500 chars) and targeted
  at the MOST RECENT note's gap only, rather than growing it cumulatively toward full coverage — a
  longer, more-complete observation makes both failure modes (goalpost-moving and fail-open) more
  likely, not less. **Fixed 2026-08-26 (stage 1, GitHub issue #145):** the `reason` string the
  judge returns alongside a fail-open `verdict=None` now reaches the caller — `cli.py
  cmd_record_result` renamed the shadowing local to `judge_reason` and, on a fail-open blocked
  pass, adds it to the returned Directive's `data["judge_reason"]`, appends it to `detail`, and
  logs an `acceptance_judge_fail_open` event via `state.log`. `scripts/lib/judge_ledger.py` was
  checked and NOT extended — it is scoped exclusively to four named hooks
  (`HOOK_NAME_BY_BASENAME`), not to this fifth, out-of-scope call site.
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
- **Fixed 2026-08-26 (stage 1, GitHub issue #145) — this escape hatch used to REFUSE on a
  non-`acceptance_review` stage (confirmed 2026-08-26, stage 7, `criterion_type: measurable`).**
  `cmd_stage_review` (`cli.py`) used to explicitly check
  `stage.criterion.criterion_type != CriterionType.ACCEPTANCE_REVIEW.value` and refuse with
  `"stage N is not acceptance_review; stage-review applies only to acceptance stages"` — but the
  judge gate that produced the deadlock (`gates.stage_review_active`) fires for **any**
  substantive-session stage regardless of `criterion_type`, so a `measurable`-criterion stage
  could be judge-deadlocked with no `stage-review --verdict override` path out at all. It now
  refuses on `not gates.stage_review_active(state)` instead — the **same predicate** the gate
  itself consumes — so the override is available for exactly the stages the gate can deadlock,
  and the message names the gate's own scoping state (`weight_class`, `AGENTCTL_STAGE_REVIEW`).
  The criterion-type-agnostic kill switch (`AGENTCTL_STAGE_REVIEW=0 agentctl record-result
  --session <sid> --status passed ...`) remains a working but strictly weaker escape — it
  proceeds WITHOUT a judge verdict and records an unattributed `JudgeBypass(kind="killswitch")`
  rather than a reviewer+note-bound `JudgeBypass(kind="override")`. In practice both escapes were
  also denied by the Claude Code auto-mode classifier when invoked by the agent itself, so the
  user had to run the command directly via a single-line script rather than the agent's own Bash
  tool.
- `record-result`'s valid flags are only `--session`, `--status {passed,failed}`, `--actual`,
  `--control`, `--observation`, `--code-ref`, `--cost-log` — argparse rejects anything else
  (e.g. a plausible-sounding `--observation-source-note` is not a real flag).
- **The predicted mitigation (short, single-gap-targeted observation) is now FALSIFIED by a
  second confirmed occurrence (2026-09-03, `published-text-writer-gate` plan, stage 5,
  `expected_result_image`/`done_criterion` ~1200-1500 chars bundling ~10 distinct facts).** A
  first observation citing only aggregate test-pass counts drew `revise` ("doesn't demonstrate
  specific permission decisions, remedy messages, fixture behaviors..."); a second, deliberately
  short (~450 char) observation that dropped the aggregate framing and instead quoted 6 literal
  `PASSED` test names mapped one-to-one to specific `done_criterion` clauses drew a **second**
  `revise` with a differently-shaped complaint ("asserts test names match criteria rather than
  demonstrating the actual observed behaviors... aggregate pass counts don't prove specific
  requirements were verified") — i.e. shortening *and* targeting the observation at concrete,
  named facts did not converge; the judge moved from "too aggregate" to "citing names isn't
  citing behavior" between the two calls. This means the mitigation's own mechanism (bundle size
  in the expected-image) is not sufficient on its own — a `done_criterion`/`expected_result_image`
  this size and this itemized appears to make the single-call judge unstable regardless of how the
  `--observation` is shaped, since no textual attestation (aggregate or literal-per-clause) reads
  to it as "demonstrating the behavior" rather than "asserting a proxy for it." Resolved both times
  via the `stage-review --verdict override` escape (2026-08-26 and 2026-09-03), not by ever
  reaching a `pass` verdict. **Open, unmitigated normative gap** — no code fix has been proposed or
  applied yet; a plausible direction (untried) is bounding the judge's demand explicitly in
  `advisor._PROMPTS["acceptance_observation"]` to "does the text name at least N concrete,
  falsifiable facts from the expected image", rather than an open-ended "is this adequate" that a
  single cheap call answers inconsistently across resubmissions of the same underlying evidence.
  This is a candidate self-improvement task in its own right (edits `advisor.py`, hence its own
  plan-approval spine) — not something a single plan's replan can fix, since the mechanism is
  shared across every SUBSTANTIVE session's judge-gated stages, not scoped to one plan.

## See also

- [[question-provenance-gate]] — the sibling plan-approval-axis gate (`premise` plugin); this
  leaf covers the resolution-axis acceptance-judge gate instead.
