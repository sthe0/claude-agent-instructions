---
name: 2026-07-21-agentctl-acceptance-judge-stale-verdict-nested-claude-unavailable
description: On a SUBSTANTIVE session with advisor-mode=substantive, record-result on an acceptance_review stage runs the fail-open acceptance judge (claude -p --model sonnet, 20s). Where nested claude -p cannot run (headless/sandboxed), the judge returns None, records NO fresh StageReview, and the gate (gates.acceptance_review_blockers) stays blocked as 'stale' against a leftover verdict bound to earlier observation bytes — every re-run of record-result reproduces 'stale' because the observation text keeps changing. Escape without gaming: record a manual stage-review --verdict pass --observation "$X" whose bytes are IDENTICAL to record-result --observation "$X"; the gate binds review.observation_sha256==sha(observation) and clears, and the None-returning auto-judge keeps the manual review instead of overwriting it. Editing the observation between stage-review and record-result is what re-staled it.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com"
refs: [gates.py:420-461, cli.py:1924-1954]
created: 2026-07-21
last_verified: 2026-10-06
---

# Acceptance-judge stale-verdict deadlock when nested claude -p is unavailable — escape via byte-identical stage-review binding

## Difficulty
Acceptance-review stages could not be recorded PASSED: the fail-open sonnet judge never produced a verdict in this environment, so record-result blocked forever as 'acceptance judge verdict is stale', deadlocking the resolution of an otherwise-done track.

## Order & criterion
record-result --status passed --observation X (judge runs, fails open, no verdict) -> gate reads leftover stale review -> BLOCK. Fix: stage-review --verdict pass --observation X (byte-identical) FIRST, then record-result --observation X (same bytes) -> sha matches -> cleared.

**Acceptance check:** acceptance_review stage flips to PASSED and node advances; verify-final reaches RESOLUTION.

## Contexts

### 2026-07-21 — initial
- Where it arose: agentctl SUBSTANTIVE session, advisor-mode=substantive, nested claude -p unavailable/timing-out; de448-throughput-loadtest track close.
- Working plan: Read gates.acceptance_review_blockers + cli.py record-result judge block; bind manual stage-review observation to the exact record-result observation bytes; do not mutate the observation between the two calls.


### 2026-09-18 — Judge reachable, verdict 'revise' on a content-free observation
- Where it arose: Filing one report-only Core backlog issue (sthe0/claude-agent-instructions#223); single in_thread stage, advisor-mode=substantive
- Working plan: Re-record the same passing result with an observation that states what the published artifact CONTAINS, section by section, rather than that the commands exited well.


### 2026-10-04 — judge revise on shifting/non-converging grounds across 6 stages, cleared only by human override
- Where it arose: an Org-tier ticket-driven agentctl session (quality/cost/latency benchmark of an internal agent product across model backends), advisor-mode=substantive, 13-stage plan. Across the full run, advisor.acceptance_judge returned revise on SIX separate stages (4, 5, 7, 8, 12, 13) — each time the judge ran successfully (not the nested-claude-unavailable/stale-verdict failure mode above) and raised a genuine, non-trivial concern, but on grounds that shifted between re-submissions and were sometimes mutually exclusive: e.g. on one stage the judge first objected that a measured figure lacked a confidence interval, then after a CI was added objected instead that the CI method itself was unstated, then after the method was named objected that the stated method did not match a different adjacent figure's method -- a moving target where satisfying the previous round's objection opened a new, unrelated one rather than converging. Each of the six was ultimately cleared only via 'agentctl stage-review --verdict override' after a human (the root/customer) read the actual evidence and judged it sufficient -- not by ever producing an observation the judge itself accepted.
- Working plan: No single mechanical escape existed (unlike the sha-binding escape above, which works because the failure mode is a STALE comparison against literal bytes). The working resolution each time was: (1) treat a second 'revise' with a DIFFERENT concern than the first as a signal the artifact/evidence is probably fine and the judge's criterion is underspecified, rather than iterating further on the artifact; (2) have the accountable human (root coordinator, with the task's actual customer where the concern was substantive) read the raw evidence directly and record an explicit 'agentctl stage-review --verdict override' with the override reasoning; (3) do NOT loop re-submitting record-result against the same judge call hoping for a different draw -- each retry cost a full sonnet call and, 6/6 times in this session, did not converge. A related, smaller finding surfaced at the SAME gate: 'agentctl record-result' only ever reports ONE blocking gate per call (code-review gate, then the acceptance-judge gate, on separate calls) even when both are failing simultaneously on the same stage -- so clearing them took twice as many round-trips as necessary; a caller cannot tell from one failed call whether a second gate is also going to block next.

### 2026-10-06 — 2026-10-06 — judge revise x5 on a green stage, user-sanctioned override (#292)
- Where it arose: Core ticket #292 (landed check after rebase landing), substantive session, 2-stage plan; stage 1 revise 5x on shifting grounds, stage 2 once
- Working plan: Asked the user via AskUserQuestion, then stage-review --verdict override --reviewer user with the observation bytes identical to record-result; posted the precedent to #204. Stage 2 passed on retry with raw command output in the observation.
## Common core & variations
**Common:** The acceptance gate binds to the --observation bytes alone. Whatever goes into --actual is invisible to the judge, so a rich --actual plus a thin --observation reads to the judge exactly like an unverified claim.

**Variations:** Distinct from the nested-claude-unavailable context above: here the judge RAN and returned a substantive 'revise' — 'the observation only confirms a command succeeded and an issue was created, but doesn't describe the actual content'. The fix is not a manual stage-review but a better observation: restate the artifact's own content against each element of the stage's expected_result_image. A second, unrelated trap followed immediately — the retry's judge subprocess exited non-zero (fail-open), which stores NO verdict yet still blocks as 'stale'; re-running the identical record-result command a second time succeeded, so a fail-open judge failure is worth one plain retry before reaching for stage-review.

The 2026-10-04 context is a THIRD, structurally different failure mode from the first two: not a stale comparison (mechanically escapable via sha-binding) and not a single thin-observation miss (escapable by writing a richer observation) but a judge that runs successfully and keeps finding genuine-sounding but NON-CONVERGING objections — 6/6 stages in one session needed a human override, 0/6 converged by revising the artifact or the observation. This has no mechanical escape by construction: `stage-review --verdict override` by an accountable human, after reading the raw evidence, is the only working exit once a second `revise` lands on different grounds than the first. Candidate self-improvement finding (not yet actioned — flagged for root/self-improvement review): a 6/6-override rate in one session is strong enough evidence to warrant checking whether `advisor.acceptance_judge`'s prompt or model needs recalibration (e.g. it may be optimizing for finding SOME objection every round rather than converging toward a stable pass/fail bar) — see `docs/operations/advisor-timeout-calibration.md` for the sibling timeout-calibration precedent of treating the advisor's own parameters as tunable, not fixed. A second, smaller finding at the same gate: `agentctl record-result` surfaces only ONE blocking gate per call (e.g. the code-review gate, then separately the acceptance-judge gate) even when both are already failing on the same stage, costing extra round-trips to discover the second blocker only after clearing the first.

## Cost
Not recorded — this leaf was salvaged from an abandoned worktree well after the originating
session closed, so the `agentctl resolve` cost figure for that session is no longer available.

## Self-critique of the agent system
Burned ~6 calls rediscovering the sha-binding before reading the gate source; should have read gates.py on the first 'stale' rather than retrying identical calls.

2026-10-04: after the second of six stages hit a 'revise' on grounds unrelated to the first, the pattern should have been named explicitly (and this leaf extended) at stage 2 of 6, not after all six had independently rediscovered the same "no mechanical escape, override after human read" resolution — a mid-session self-improvement trigger (two-or-more process corrections in a row) was live from the second occurrence.
