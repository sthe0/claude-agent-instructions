---
name: behavioral-conformance-control
description: Conformance — whether the coordinator's actual behavior followed the instructions and the plan's expected actions — is a first-class verification axis, checked at every step boundary and at resolution, not only reported when the user notices a gap.
schema: leaf/v1
type: reference
created: 2026-07-22
last_verified: 2026-07-22
---

## Difficulty

A task can satisfy every artifact-level check — the code compiles, the plan's stages are marked PASSED, the deliverable matches the spec — while silently diverging on the *behavior* axis: an instructed specialization was never spawned, a planned action was never taken, or an implicit precondition of an action went unmet (e.g. a plan reviewer recorded a verdict without ever being able to read the plan it signed off). Nothing controlled that divergence — not continuously during the task, not at resolution — so it surfaced only when the user happened to notice. A difficulty is a divergence between expected and actual state; the coordinator's own behavior is itself a verification object, and until now it had no owner.

## Guidance

**1. Generalization — every expected action is a conformance object.** Not only an instructed specialization spawn, but any planned action, and any *implicit precondition* of an action (that a reviewer actually read what it reviewed, that a fetched artifact was actually fetched) is a thing whose actual occurrence can diverge from its expectation. A divergence on any of them is a difficulty like any other.

**2. Check at every step boundary AND at resolution — including after the task closes.** At each step boundary, compare actual behavior against (a) the instructions in general and (b) the plan's expected/planned actions for that step. A mismatch is declared immediately as a difficulty (Expected/Actual/Mismatch), not silently passed over — and the check is not retired once the task is marked done: a conformance gap can be (and in the exemplar below, was) noticed only in retrospect, and that still counts.

**3. Two-layer coverage — perception net + mechanized slice.** "Every expected action" is not statically enumerable — no engine can list in advance everything a coordinator might have been expected to do. So the general net is *this prose rule*, a perception the model applies at each boundary; the decidable slice is mechanized separately as the conformance-obligations ledger (`scripts/agentctl/plugins_obligations.py`): every blocking `PluginDirective` registered in its `_DISCHARGE` table is minted as a tracked obligation at the `plugins.fire()` seam and must be discharged before a SUBSTANTIVE task's resolution gate opens. This mirrors CLAUDE.md's "separate rule from perception" principle — mechanize the decidable part, keep the model for the rest, and name the boundary.

**4. Extension recipe — how a newly-noticed case gets mechanized.** The reviewer-attested plan-digest (plan-review's `--plan-digest`: the reviewer supplies the sha256 it computed from its own read, and the engine binds a `pass` verdict only when that attested digest equals the live plan bytes at record time) is the first worked instance of turning a newly-identified implicit-precondition gap into a mechanical check — a reviewer that cannot access the current plan cannot produce a matching digest, so it cannot record a binding pass, closing the exemplar's sibling-session gap (a verdict recorded by a reviewer with no read access). The attestation establishes that the reviewer processed the exact current bytes, not that it comprehended them — comprehension stays with the perception net (point 3); naming that boundary honestly is itself an instance of this rule. To mechanize the next one found by this rule, either: (a) attach an attestation the engine cross-checks against live state (the plan-digest template — the actor supplies proof-of-action, the engine verifies it), or (b) if the gap is a blocking directive the engine already emits, register its action in the obligations `_DISCHARGE` table and update the corresponding static pin in `scripts/verify-agentctl.py`. Until either lands, the case is still caught by this rule's perception net — mechanization is an upgrade in confidence, not a precondition for the difficulty to count.

**Exemplar.** A prior task expected two specialist spawns at defined moments (a thinker plan-review, a code-reviewer pass on a developer stage); neither ran, and no conformance check — continuous or at resolution — caught the gap, not even after the task was reported done. Separately, a sibling plan-review session recorded a `pass` verdict from a reviewer that lacked read access to the plan file it was reviewing — the verdict existed, but the action it was supposed to attest (reading the plan) never happened.

## See also

- [[determinize-required-specialist-dispatch]] — the reactive gates (`plan_review_blockers`, `code_review_blockers`) whose undischarged state is exactly what the obligations ledger tracks.
- [[question-provenance-gate]] — the `premise` plugin, the sibling gate this rule's mechanized slice reuses as a template (auto-activate on SUBSTANTIVE, a plugin-owned gate over a ledger bag).
- [`scripts/agentctl/plugins_obligations.py`](../../scripts/agentctl/plugins_obligations.py) — the mechanized slice (point 3): selective mint of `_DISCHARGE`-registered blocking directives, resolution-gate guardian.
