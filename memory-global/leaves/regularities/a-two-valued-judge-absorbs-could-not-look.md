---
name: a-two-valued-judge-absorbs-could-not-look
description: A judge that can only answer pass or fail still meets cases where it could not observe; those cases do not disappear but are silently reported as one of the two answers, so an unrun check reads as passed or a lost signal reads as failed.
type: reference
schema: regularity/v1
generality: 3
domain: development
induced_from: [2026-08-18-fail-open-gate-exit-zero-and-underfetched-listing, 2026-07-21-agentctl-acceptance-judge-stale-verdict-nested-claude-unavailable, 2026-07-01-monitor-self-defeats-verify-its-own-run, 2026-07-04-spawn-budget-death-forensics-before-respawn, 2026-07-04-topological-gate-when-signal-unobservable, 2026-07-01-imagined-permission-vs-deterministic-scoped-wrapper, 2026-10-12-stage-control-liveness-nondeterminism]
created: 2026-10-27
last_verified: 2026-10-27
---
# A judge that can only answer pass or fail still meets cases where it could not observe

## Regularity

A judge built with only two answers, pass and fail, still meets cases in which it could not observe at all — the ruleset is not installed, the nested model is unavailable, the monitor crashed, the budget ran out before the marker was written, the client dropped the text it counts. Those cases do not disappear: they are reported as one of the two answers, silently. Which one depends on the default the judge falls back to — a fail-open default turns an unrun check into a pass, a fail-closed default turns a lost signal into a failure of the work — and in both directions the verdict reads exactly like a real observation.

## Generality

Level 3 — ranges over any judge — a gate, a monitor, an acceptance judge, a dispatch that reads a marker, an agent asserting it cannot act — whose observation can fail independently of the thing it judges. It is not claimed for a judge whose observation cannot fail separately from its subject (a pure function of an in-memory value).

## Refutation

A two-valued judge observed over its real failure modes for which no could-not-observe case ever arose, or for which every such case was surfaced distinctly without a third answer having been built, would show the absorption does not happen for that judge.

## Induced from

- [[2026-07-04-spawn-budget-death-forensics-before-respawn]] — § 2026-08-10 (the judge-abort verdict)
  is what induced the naming corollary. That stage produced two artifacts side by side in one diff —
  one named after the probe that captured it, one named after its subject — and nothing but the
  author's own memory of who wrote which distinguished "collected" from "asserted". The verdict
  recorded there (the ledger cannot see an abort it was never wired to observe) is the same fault on
  the evidence-domain axis that this principle states.
- [[2026-10-12-stage-control-liveness-nondeterminism]] — three contexts: a network read whose failure was scored as an absent artifact (repaired by a pinned-key read, bounded attempts and a distinct unreadable outcome), a single non-reproducing exit from a live-API final check that every manual re-run contradicted, and a local suite block that exited non-zero once and cleanly five times after, together with the finding that the loop guard digests the command's static text and so cannot tell a repeat from a new failure.
