---
name: 2026-08-04-no-circuit-breaker-on-verification-effort
description: A task classified SUBSTANTIVE kept that class after the user contracted it to a few minutes of work; the engine's gates all guard against under-verification and none against over-verification, so ten locally-justified plan-review rounds ran on a plan that no longer warranted any.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user rated the outcome 2/5 on 2026-08-04 and asked for a mechanism, not a promise"
tier: 1
created: 2026-08-04
last_verified: 2026-08-04
---

# Verification effort had no upper bound, so a collapsed task kept its heavyweight class

## Difficulty
Every gate in the agentctl spine is asymmetric: it blocks progress until enough verification has happened, and nothing anywhere bounds how much verification may happen. Each of ten thinker plan-review rounds was LOCALLY justified — the reviewer found a real defect nearly every round — but no actor held the GLOBAL view (spend so far, remaining work, whether the task still warranted a plan at all). Compounding it, the weight class is assigned once at classify and never revisited, so when the user dropped three of four stages and said the task was solved, the mandatory-review machinery for SUBSTANTIVE work kept applying to what the triage table calls a small change. The user's complaint was not that the work was wrong but that supervising it defeated the purpose of delegating it.

## Order & criterion
Track the task's ACCUMULATED ACTUAL effort against the estimate re-derived from the CURRENT plan, across four scales — spend, count of user interactions, active wall-clock, replan count — every actual measured from the arming point (a baseline snapshotted when the plan is approved); when the actual exceeds the estimate past a configured multiple, the engine enters the existing declare → investigate → critique cycle by itself, on the reasoning that a norm this far off is visibly missing something essential about the real situation.

Two details carry the design. **Re-derivation at every replan** is the load-bearing one: the estimate falls when the plan contracts while the accumulated actual keeps running, so a collapsing task's ratio rises instead of resetting — which is exactly the case that produced this leaf. And **the terminal is diagnose, never block-and-ask**: the standing requirement behind the order is that the user stop having to supervise the agent, so a mechanism that stops to request permission reinstates the very supervision it was built to remove.

Rejected as crutches: the earlier three-part proposal — (A) a spend circuit breaker hard-blocking further spawns at 2x declared, (B) a review-round cap blocking a third round on one plan, (C) forced re-classification when a replan contracts the plan. Rejected because A, B and C are three proxies for one quantity (effort diverging from its own norm) and two of them terminate in block-and-ask, which is the wrong terminal.

**Acceptance check:** a session whose actual effort passes the multiple on any armed scale enters DIAGNOSING on its own, with no user prompt at the fire site, and stays silent afterwards until a replan sets a new estimate.

## Contexts

### 2026-08-04 — initial
- Where it arose: the0.fun VPN repair (0-rpqowix2 over MTS mobile data); the fix was two link fields, delivered after ten plan-review rounds
- Working plan: /Users/the0/.claude-agent/plans/the0-vpn-mts-mobile.toml

## Cost
**$11.23 across 12 `thinker` spawns, 39 min of spawn wall-clock**, for a fix that was two link fields — every spawn a plan review on one plan, none a code or research spawn. Figures from `~/.local/log/claude-spawn-costs.jsonl` filtered to `plan_path=/Users/the0/.claude-agent/plans/the0-vpn-mts-mobile.toml` (the same ledger `scripts/cost-report.py` reads). Main-thread tokens are not in that figure and are not attributed anywhere, so $11.23 is a floor, not the total. That single ratio — a two-field fix costing twelve review rounds — is the measurement the trigger above exists to make automatic.

## Self-critique of the agent system
I noticed the disproportion myself after round 6 and contracted the plan, then ran four more rounds anyway because the engine's plan_review gate demanded a passing verdict before replan and the pass had to be digest-attested. That is the tell: I could see the loop and could not leave it, which is exactly what makes it a missing mechanism rather than a lapse of attention. I also should have re-run classify the moment the user said the task was solved.
