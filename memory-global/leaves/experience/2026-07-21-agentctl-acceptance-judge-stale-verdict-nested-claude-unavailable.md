---
name: 2026-07-21-agentctl-acceptance-judge-stale-verdict-nested-claude-unavailable
description: On a SUBSTANTIVE session with advisor-mode=substantive, record-result on an acceptance_review stage runs the fail-open acceptance judge (claude -p --model sonnet, 20s). Where nested claude -p cannot run (headless/sandboxed), the judge returns None, records NO fresh StageReview, and the gate (gates.acceptance_review_blockers) stays blocked as 'stale' against a leftover verdict bound to earlier observation bytes — every re-run of record-result reproduces 'stale' because the observation text keeps changing. Escape without gaming: record a manual stage-review --verdict pass --observation "$X" whose bytes are IDENTICAL to record-result --observation "$X"; the gate binds review.observation_sha256==sha(observation) and clears, and the None-returning auto-judge keeps the manual review instead of overwriting it. Editing the observation between stage-review and record-result is what re-staled it.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com"
refs: [gates.py:420-461, cli.py:1924-1954]
created: 2026-07-21
last_verified: 2026-09-18
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

## Common core & variations
**Common:** The acceptance gate binds to the --observation bytes alone. Whatever goes into --actual is invisible to the judge, so a rich --actual plus a thin --observation reads to the judge exactly like an unverified claim.

**Variations:** Distinct from the nested-claude-unavailable context above: here the judge RAN and returned a substantive 'revise' — 'the observation only confirms a command succeeded and an issue was created, but doesn't describe the actual content'. The fix is not a manual stage-review but a better observation: restate the artifact's own content against each element of the stage's expected_result_image. A second, unrelated trap followed immediately — the retry's judge subprocess exited non-zero (fail-open), which stores NO verdict yet still blocks as 'stale'; re-running the identical record-result command a second time succeeded, so a fail-open judge failure is worth one plain retry before reaching for stage-review.

## Cost
Not recorded — this leaf was salvaged from an abandoned worktree well after the originating
session closed, so the `agentctl resolve` cost figure for that session is no longer available.

## Self-critique of the agent system
Burned ~6 calls rediscovering the sha-binding before reading the gate source; should have read gates.py on the first 'stale' rather than retrying identical calls.
