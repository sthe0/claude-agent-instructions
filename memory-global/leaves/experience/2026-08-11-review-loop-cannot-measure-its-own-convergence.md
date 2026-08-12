---
name: 2026-08-11-review-loop-cannot-measure-its-own-convergence
description: Difficulty — a repeated independent plan-review loop ran 12 rounds ($18.89, ~72 min) on one plan and nobody could say whether it was converging, because the machinery retains round identity and verdict for 71/71 recorded rounds but fix size for 15/71, finding COUNT for 5/71 and finding TEXT for 0/71: --concern strings live in a single PlanReview slot overwritten by the next review, and the durable history event carried only four keys. Three consequences, each general. (1) A volume-law hypothesis about review non-convergence is UNANSWERABLE on such a record — not refuted, unanswerable — because the record does not retain the two variables it relates; instrument BEFORE theorizing. (2) The loop has no verb distinguishing 'this plan has a defect' from 'this study cannot be done as specified': both arrive as BLOCKING and both route to revise-and-re-review, so a premise-invalidating finding raised in round 2 was re-litigated for eight more rounds and only then recorded as a cut against the order (the disposition vocabulary compounds it — order-dispose admits only covered|cut, no partially-covered, no escalated). (3) A THIRD non-convergence mechanism, in neither the volume nor the reviewer hypothesis: repair is itself an authoring act at the density where authoring introduces defects, so late rounds show IATROGENIC findings — the defect sits inside the passage the previous repair edited — and the finding count falls to a NON-ZERO FLOOR; a loop with a non-zero floor plus a hard pass-required gate does not terminate by construction, it terminates when someone stops it. That predicts the opposite remedy from the volume law: shrinking the artifact does not help, FREEZING A FINISHED PART does. And a fourth, structural: review reads the ARTIFACT while execution reads the MACHINERY, so artifact-to-machinery fit defects are invisible to review at any number of rounds — two such defects here survived 11 and 12 rounds respectively and were found by the first execution attempt each time.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [/Users/the0/.claude-agent/plans/plan-convergence-evidence/cause-account.md, /Users/the0/.claude-agent/plans/plan-convergence-revision-log.md]
created: 2026-08-11
last_verified: 2026-08-11
---

# A plan-review loop that records no finding data cannot measure its own convergence, and has no verb for 'the study cannot be done'

## Difficulty
A repeated independent review loop on a large plan does not visibly converge, and the loop's own machinery retains nothing that would let anyone tell whether it is converging or why.

## Order & criterion
Establish the CAUSE of large-plan non-convergence before designing any remedy (user's explicit ordering); test the user's law-of-large-numbers hypothesis, and say plainly if it is wrong or incomplete.

**Acceptance check:** acceptance-review — a written account, every number traceable to a named row, reporting the availability census as the leading result, the single-series measurement with its limits, and what the evidence does and does not license about the proposed remedy. Accepted by the user on review, quality 3/5.

## Contexts

### 2026-08-11 — plan-convergence-cause: 12 rounds of review on one plan, then a census of all 71 recorded rounds
- Where it arose: agentctl sessions on ~/claude-agent-instructions; population = 71 review rounds across 12 series pinned in rounds-manifest-pinned.tsv
- Working plan: Stage 1 built the availability census and per-series convergence table against root-pinned manifests (71/15/5/0; the 42 surviving review artifacts are DISJOINT from the 71 recorded rounds). Stage 2 instrumented the one series retaining both H1 variables — this plan's own six rounds — and wrote the account: r(bytes, BLOCKING) = -0.30 at n = 6, -0.16 at n = 5, i.e. weakly OPPOSITE to H1, reported with an explicit refusal to generalize (n = 6; one author; and the series is instrumented precisely BECAUSE it was under observation). Stage 3 instrumented the loop prospectively so the three missing quantities are recorded from the next round on — plan_sha256, plan_bytes, concern text, note and two optional finding counts onto the durable plan_review event — landed as 21ba780. Population-scale facts that DO survive: rounds per series 19/16/9/6/5/5/3/3/2/1/1/1; 11 of 12 series terminate in pass; 37 revise / 33 pass / 1 override over 71 rounds.

## Cost
12 plan-review rounds $18.89 / ~72 min (BLOCKING series 4,4,4,4,3,3,1,1,0,0,0,0); stage 3 developer spawn $3.62 / 14.6 min; code review $2.06. Ten rounds passed before a single measurement ran.

## Self-critique of the agent system
The account's section (h) is written in the future tense about stage 3, which had not yet executed when it was written and has since executed; left unedited and disclosed instead, because editing a closed stage's artifact for cosmetics is the iatrogenic mechanism the account itself describes. Nine NON-BLOCKING findings from round 12 were declined and recorded as concerns rather than fixed, on the same ground. Two of them are substantive and remain: nothing asserts the base suite run actually happened, and stage 3's capability_required over-corrects past the fact. The measurement is self-administered throughout: the same author and script produced both the literals and the controls that check them.
