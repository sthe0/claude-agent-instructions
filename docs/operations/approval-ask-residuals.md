# approval_ask residuals (judge-ceiling-drift, stage 5)

Two symptoms survive the ceiling correction landed elsewhere in this plan.
Both are diagnosed here against the live judge execution ledger and retained
agentctl session state, per the judge-ceiling-drift plan's stage 5 method:
each residual ends in exactly one of `fixed:` / `filed:` / `not_reconstructible:`,
never a speculative narrative.

## Residual A — a hung-call tail at approval_ask, ceiling=30

**Evidence** (live `judge-usage-ledger.jsonl`, ceiling=30, n=208 `decided` rows).
Completed population (n=203, `timed_out: false`): max **22.05s**, p90 **17.55s**.
Timed-out population (n=5, `timed_out: true`, `reason: "judge timed out
(fail-open)"`): 29.78s, 29.75s, 29.20s, 28.84s, 29.72s — all within ~1s of the
30s ceiling and well outside the completed population's max. This is not a
slightly-too-low ceiling truncating slow calls; it is a distinct population
consistent with a stuck/wedged subprocess, and raising the ceiling further
would not help a call that is genuinely stuck — it would only make it fail
open later.

All 5 kills belong to the same source session, `7514dd40-b947-4cc5-84aa-
983476c2515c`, spread across 2026-08-20 through 2026-08-27 (no other
approval_ask@30 session has a single timeout). Per the plan's step 2, a
bounded single retry inside the existing budget was evaluated and does not
fit: `_APPROVAL_ASK_JUDGE_BUDGET_S = 30` in `scripts/hook-plan-delivery-
gate.py`, and a single killed call already consumes up to ~29.8s of it,
leaving no room for a second call inside the same fixed budget without
raising the budget or adding a smaller-scoped remedy — both are design
decisions outside this diagnosis stage's scope.

residual A: filed: #209 approval_ask: cluster of near-ceiling kills (28.8-29.8s) all in one session, not fixable by raising the ceiling

## Residual B — a stale-delivery block with no approval_ask decision in front of it

Issue #110's 2026-08-19 17:08 comment reports a second "delivery proof is
stale" block, unblocked only via `confirm-delivery --escape-reason
transcript_unverifiable`.

**What is refuted.** An approval_ask timeout does not explain the block:
between the approval_ask decisions at 16:31:41 and 18:50:49, the ledger holds
no approval_ask `decided` row, and no approval_ask call was even `entered`
before the 17:08 report. Nor is the window silent — 24 calls by other judges
run inside it (25 counting the boundary approval_ask timeout that opens the
window), so "the ledger was quiet" is also false and is not used as a
finding here.

**What is true, at the width the evidence supports.** One approval_ask
invocation, `503460a0` (hook `plan_delivery`, source session `7514dd40-
b947-4cc5-84aa-983476c2515c`), entered and started at
2026-08-19T18:08:50.92 and produced no terminal row (no `decided`/`final`/
`emitted`) — outcome 6, "hook killed by the harness during the call." This
is ~1h after the 17:08 report and its epoch (1787162930) precedes all five
of residual A's timeout timestamps (earliest 1787230027, 2026-08-20 12:47),
so it is not itself the reported block and cannot be read as its cause.

**New finding.** Session `7514dd40`'s retained agentctl state shows a `reset`
from an unrelated prior task (`de495-macro188-provenance-fix`) to
`de495-fix-codeact-multiblock`, whose own plan-approval timestamp
(`verified_ts: 1787589165`, 2026-08-24) postdates the 2026-08-19 18:08:50
outcome-6 row by ~5 days. At the moment invocation `503460a0` ran, session
`7514dd40` was executing an entirely unrelated task with no connection to
#110 or the delivery-gate investigation — agentctl sessions are long-lived
and reused across unrelated tasks over time (independently confirmed: this
very stage's own session, `fa80b9a9-7a46-42a9-b78e-039f143de62d`, is a
long-lived cross-task session predating this harness session per its own
delivery receipt). `503460a0` is ordinary hook traffic from an unrelated
task that happens to share a session id, not evidence about the 17:08 block.

**Disposition.** The 17:08 block itself cannot be reconstructed from what
was retained: agentctl session state is mutated in place per session, not
versioned per task run, so whatever state the reporting session held at
2026-08-19 17:08 has since been overwritten by every later task run in that
same session, and no harness-side Claude Code transcript for that exact
session/turn was found under either session id examined. What would have
been needed: a per-task-run snapshot of agentctl session state (or at
minimum the presentation receipt and delivery stamp sidecars) taken at
approval time and retained independently of later overwrites, plus the
harness transcript for that session/turn.

residual B: not_reconstructible: would require a per-task-run snapshot of agentctl session state (or the presentation receipt + delivery stamp sidecars) taken at approval time and retained independently of later overwrites, plus the harness-side transcript for that exact session/turn — session state is mutated in place and neither was retained past 2026-08-19

## Same-phenomenon check (plan step 3c)

Both residuals implicate session `7514dd40`, so the one-phenomenon
hypothesis was tested directly rather than assumed. Two observables were
compared: (1) ledger row shape and (2) timing. On shape, residual A's five
kills are `decided` rows with `timed_out: true` — the hook's own timeout
logic ran to completion and logged a fail-open verdict; `503460a0` is
`entered`/`started` with no `decided` row at all — the process was killed
from outside before its own logic ever produced a verdict. These are
different failure shapes at the ledger level, not two views of the same one.
On timing, `503460a0` (2026-08-19T18:08:50, epoch 1787162930) precedes every
one of residual A's five timeouts (earliest 2026-08-20T12:47:07, epoch
1787230027) by at least 18 hours, and the shared session id is explained by
session `7514dd40` being reused across unrelated tasks over more than a
week (2026-08-19 through 2026-08-27) rather than by one incident recurring
within a tight window.

residuals A,B: same_family: no: residual A rows are `decided`/`timed_out: true` (hook completed its own fail-open timeout logic); `503460a0` is `entered`/`started` with no terminal row at all (killed externally before any hook logic ran), and it precedes every residual-A timeout by >=18h under a session id independently shown to be reused across unrelated tasks over more than a week
