---
name: 2026-06-30-policy-as-code-split-judgment-defer-blocked-layer
description: Converting a verbal 'don't bypass review outside your sandbox' rule into deterministic code: the durable split is mechanism (classification, bypass-refusal, poll cadence, closure-gating = CODE) vs judgment (reply-to-comment, fix-code = MODEL); and when the deepest enforcement layer needs a platform capability that turns out absent (the gate can't name the bare identity it must gate on -> fail-open), ship the verifiable client-side layer now and defer the server-side layer with a recorded recipe instead of blocking.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [https://a.yandex-team.ru/review/14170183, trunk r20151520, Core dae4558..a0bdbf1, leaves/pr-land-review-gate.md]
created: 2026-06-30
last_verified: 2026-06-30
last_accessed: 2026-06-30
---

# Verbal policy -> auto-applying code: split judgment from mechanism; ship the verifiable layer, defer the platform-blocked one

## Difficulty
A behavioral rule that lives only in prose ('inside your own namespace, self-landing without review is fine; outside it, never bypass review/CI') is forgettable and unenforced precisely under load. Naive 'encode it' attempts then fail two ways: (a) they try to encode the cognitive part (judging a review comment) and stall, or (b) the deepest enforcement layer is assumed available and turns out platform-blocked, threatening to sink the whole task.

## Order & criterion
1) Split the policy by what needs judgment. Mechanism -> code: a pure boundary classifier (path-prefix of the changed file-set vs the actor's owned namespace; empty/anomalous set never classifies as owned -> fail-closed), a tool-layer PreToolUse deny of a bypass token outside the owned namespace, a detached zero-token poller emitting MERGED/NEW_COMMENTS/CHECK_FAILED, closure gated on the MERGED marker. Judgment -> model: only reply-to-comment and fix-code. 2) Prove the code layer in isolation with a hermetic test (classifier+gate, no live landing) BEFORE wiring. 3) When the server-side layer hit a platform limit (review config can't name a bare login -> silently resolves to nobody -> fail-open), do not block: ship the verified client-side layer, defer the server-side one with a written, docs-verified recipe + the residual it leaves.

**Acceptance check:** acceptance-review. Live deny/allow on the COMPOSED hook in a real mount (colleague-bypass-outside -> deny, owner-self-ship-inside -> allow, unrelated cmd -> allow), hermetic test 26/0, all artifacts confirmed on trunk via a refreshed VCS ref ('posted != published': the lazy local trunk ref was stale right after merge and had to be re-pulled before ls-tree showed the files).

## Contexts

### 2026-06-30 — agent-system / policy-as-code
- Where it arose: 2026-06-30 deepagent workspace. Convert the PR-landing rule into code. Landed Layer B on trunk (self-ship PR merged r20151520); pushed an org-neutral generalization kernel to Core (long-job-monitoring.md). Layer A (server-side review config) deferred per user decision because the review system cannot name a bare login.
- Working plan: 7 stages: (1) login-aware pure classifier, (2) PreToolUse bypass-deny gate, (3) detached monitor trio, (4) hermetic test, (5) wire hooks + compose + live E2E, (6) project-memory leaf (Layer B deployed + deferred Layer-A ABC recipe), (7) org-neutral Core kernel. Stages 1-4 built+verified independently; 5-7 in-thread under the in-context carve-out.

## Cost
0 specialist spawns (`agentctl resolve` spawn_count=0) — stages 1–4 were built by a single earlier developer pass, stages 5–7 done in-thread under the in-context carve-out. Spanned one context-compaction boundary (the DIAGNOSING recovery re-ran declare/investigate/critique from scratch). Per-stage USD/duration not split (in-thread tokens are not attributed per stage). Dominant cost driver: the platform-capability discovery for Layer A (two yandex-guru doc consults) and the replan coverage-gate iteration (verbatim-invariant edits), not the code itself.

## Self-critique of the agent system
Recovered the engine from DIAGNOSING across a context boundary (the difficulty record does not survive compaction; re-ran declare/investigate/critique). The replan coverage gate forced verbatim invariant strings into stage text — a reminder that the gate checks plain-substring landing, not semantics. Watch: nearly reported the trunk landing from a stale local ref; only a re-pull made ls-tree truthful.

## See also
- [[2026-06-24-prose-to-code-migration-consumer-and-superset]] — sibling: the *failure modes* of prose→code migration (dead code / information loss). This leaf records the complementary *design* lesson (the judgment-vs-mechanism split + defer-the-blocked-layer move).
- [[long-job-monitoring]] — the org-neutral kernel this task pushed to Core: the landing-gate generalization rides the same detached-poller recipe.
- [[2026-06-04-verify-load-bearing-axis]] — the "posted ≠ published" stale-ref check applied here (re-pull before trusting `ls-tree`).
