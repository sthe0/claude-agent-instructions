---
name: 2026-08-04-no-circuit-breaker-on-verification-effort
description: A task classified SUBSTANTIVE kept that class after the user contracted it to a few minutes of work; the engine's gates all guard against under-verification and none against over-verification, so ten locally-justified plan-review rounds ran on a plan that no longer warranted any.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user rated the outcome 2/5 on 2026-08-04 and asked for a mechanism, not a promise; on 2026-08-05 the user confirmed the mechanism itself resolved at 4/5"
tier: 1
created: 2026-08-04
last_verified: 2026-08-05
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


### 2026-08-05 — the mechanism built
- Where it arose: agentctl engine, worktree claude-agent-instructions-effort-divergence-trigger; 6 stages, 16 commits, +3383/-72 across 34 files
- Working plan: /Users/the0/.claude-agent/plans/effort-divergence-trigger.v2.toml

## Common core & variations
**Common:** The same asymmetry, now closed on one side: a gate that bounds how much verification may happen, not only how little. The order's two load-bearing details survived implementation intact — re-derivation of the estimate at every replan, and a terminal that diagnoses instead of asking.

**Variations:** Where the first context OBSERVED the difficulty, this one BUILT the detector, and building it reproduced the difficulty at 2.07x: estimate $21 / 105 min against an attributed actual of $43.51, with stage 4 alone taking three full code-review rounds. Under the shipped 5x multiple that would not have fired — the first real calibration point for the threshold, and evidence the value is not obviously too low. Three implementation facts worth carrying: (1) the trigger could not fire on its own session, because arm() runs at approve and this session passed approve before effort.py existed — a retrofit artifact, but it means the mechanism has never been observed firing in the wild and its first live firing is still unverified; (2) the interactions scale shipped accounting-only (effort-absolute-interactions = 0) because a plan's intended interaction count is not derivable at all, so any threshold now would be a guess dressed as a derivation; (3) a check can pass VACUOUSLY for exactly the artifact it was written for — verify-memory-index.py's _leaf_files prefers the git-tracked set, so the new leaf was invisible to it until staged, and this same class of defect had been rejected three times in code review earlier in this very task.

## Cost

**2026-08-04 — $11.23 across 12 `thinker` spawns, 39 min of spawn wall-clock**, for a fix that was two link fields — every spawn a plan review on one plan, none a code or research spawn. Figures from `~/.local/log/claude-spawn-costs.jsonl` filtered to `plan_path=/Users/the0/.claude-agent/plans/the0-vpn-mts-mobile.toml` (the same ledger `scripts/cost-report.py` reads). Main-thread tokens are not in that figure and are not attributed anywhere, so $11.23 is a floor, not the total. That single ratio — a two-field fix costing twelve review rounds — is the measurement the trigger above exists to make automatic.

**2026-08-05 — $58.98 across 14 spawns, 155 min of spawn wall-clock**, split `developer` 5 / $43.85, `thinker` 7 / $6.28, `code-reviewer` 2 / $8.84. Also a floor: the third stage-4 review round returned MALFORMED and is not in the ledger, and main-thread tokens are again unattributed.

Two things the split by `plan_path` shows. The plan was rewritten v1 → v2 mid-task, so the ledger splits **$15.47 / 3 spawns** under the v1 path from **$43.51 / 11 spawns** under v2 — and $43.51 is precisely what the engine's own spend accumulator reported, since it sums by `plan_path` alone. Against the v2 plan's declared **$21 / 105 min**, that is **2.07×** — real, and under the shipped `effort-divergence-multiple` of 5. The overrun that produced this leaf is therefore visible in the very ledger the detector reads, at a ratio the detector would not have flagged.

## Self-critique of the agent system

**2026-08-04.** I noticed the disproportion myself after round 6 and contracted the plan, then ran four more rounds anyway because the engine's plan_review gate demanded a passing verdict before replan and the pass had to be digest-attested. That is the tell: I could see the loop and could not leave it, which is exactly what makes it a missing mechanism rather than a lapse of attention. I also should have re-run classify the moment the user said the task was solved.

**2026-08-05.** Three residuals, stated rather than fixed. (1) **The detector has never been observed firing.** Every test is synthetic; the one real session available — this one — could not arm, because `arm()` runs at `approve` and the session passed `approve` before the code existed. Whatever the tests prove, "it fires in the wild" is not among it, and the first live firing is the observation that would close this. (2) **The generalization behind the fix was not embodied, only instantiated.** The recurring shape is *establish a baseline at the moment a norm is adopted, then measure against the norm as re-derived, not as first written* — `effort.py` implements exactly one instance of it (effort against a plan), and nothing in the engine makes the next such measurement cheap. Recorded here rather than built, deliberately: one instance is not yet evidence for a primitive. (3) **I shipped a vacuous check into my own final stage** — `verify-memory-index` could not see the new leaf until it was staged — after rejecting that same class of defect three times in review during this task. Reviewing for a defect is not the same competence as not committing it, and the thing that caught it was a probe I ran on suspicion, not any gate.
