---
name: formalization-ladder-l1-l3
description: The L1-L3 formalization ladder for a reasoning/research deliverable — a criterion for when to climb and an explicit L3 refusal criterion — plus the honest enumeration residual the claim-provenance ledger cannot close.
schema: leaf/v1
type: reference
created: 2026-07-14
last_verified: 2026-07-14
---

## Difficulty

A reasoning/research task can be "verified" at wildly different depths, and both extremes fail. Verify too shallow — accept prose because it reads confidently — and fabricated claims (a decision, a judgment, a number invented by proximity and presented as fact) ship as conclusions. Verify too deep — reach for LEAN / calculus of constructions / combinatory logic on an *empirical* claim — and effort is spent trying to prove the unprovable: a load or cost measurement has no formal derivation to check, so a proof engine adds ceremony without adding grounding. Without a stated ladder *and a stop rule*, the agent oscillates between ungrounded confidence and over-formalization, and cannot say which rung a given claim actually needs.

## Guidance

Treat formalization as a three-rung ladder. Each rung names **when to climb to it** and, for the top, **when to refuse**.

**L1 — bookkeeping / type-check (the claim ledger). ~80% of the value; always, for every reasoning/research deliverable.** Every load-bearing claim — a **decision** or **judgment**, not numbers only — must be grounded (`axiom`, cited to a source), derived (`derivation`, from other recorded claims), or explicitly marked (`assumption`). The deliverable is not resolved until the ledger *closes*: no free claim presented as fact, and every enumeration candidate the cross-check raised is recorded or dismissed. This is the Curry-Howard framing — a fabricated ("додуманное") claim is a **free variable in a term asserted to be closed**; closure is the type-check, and it is decidable and LLM-free once the claims are typed. Arm it at `classify --deliverable-kind reasoning|mixed`.

**L2 — decidable re-computation. Climb here for a numeric derivation.** Recompute the number from its measured inputs and show the formula plus the inputs, so a reader re-runs the arithmetic. Applies only where a claim *is* a derivation from measured quantities (e.g. quota tokens/hour from λ and window). It does not apply to a judgment or a raw measurement — those are L1 axioms, grounded by citation, not by recomputation.

**L3 — full formalization (LEAN / CoC / combinatory logic). Rare; climb only for a genuinely formal derivation whose inference-step validity is in doubt.** LEAN's real surplus is checking that each inference step in a **formal** derivation is valid — it is *orthogonal* to grounding an empirical axiom. **Explicit refusal criterion: empirical claims — load, cost, latency, quality measurements — are NOT formalizable; do not lift them to L3.** A proof assistant cannot make a measured 0.737 or a $42–50k/mo estimate "more true"; the truth of an empirical axiom rests on the measurement, which L1 grounds by citation. Refusing L3 on empirics is the discipline that prevents the over-formalization failure mode: a ladder without a stop rule invites proving the unprovable.

**Why regex enumeration was rejected.** A regex over the deliverable text is a *formal proxy* that swaps the substantive enumeration of decisions and judgments for surface matching — it "types" strings, not claims. It would report closure on syntax while missing a load-bearing judgment phrased in prose the pattern never anticipated, and flag innocuous matches as claims. Enumeration of what is load-bearing is **perception** (the model's job), not a lexical rule; mechanizing it as regex is the crutch this ladder refuses. The mechanized part is the *closure type-check over already-typed claims* (L1), not the discovery of which sentences are claims.

**The honest enumeration residual — the blind spot Layer B discipline still owns.** The advisory-blocking cross-check (an independent second reading that raises decision/judgment candidates, each of which must be dispositioned) **narrows** enumeration; it does not **close** it. Four residuals survive, and none is caught by structure:

1. **Recall < 100%.** A claim that neither the author nor the independent cross-check surfaces still escapes — the cross-check widens recall, it does not guarantee it.
2. **DECOY.** A load-bearing claim added only to clear the `count > 0` check while the real claims are (wrongly) marked non-load-bearing passes the structural gate — structure counts claims, it does not weigh their loadedness.
3. **Junk-reason dismiss.** A raised candidate can be dismissed with a meaningless reason; the reason string is free text and **cannot be content-checked** — that is perception. The resolve observer surfaces dismiss reasons for audit but cannot guarantee their honesty.
4. **Perception ceiling generally.** Every step that decides *what is load-bearing* or *whether a reason is honest* is the model's judgment; the mechanism can force the questions to be asked and recorded, never that they were answered truthfully.

Name these as the enumeration-honesty blind spot that the coordinator's own discipline (not the mechanism) must cover. The mechanism's guarantee is narrow and real — closure over the *typed, recorded* claims is fail-closed — but it is not a guarantee that the claim set is *complete* or *honestly dispositioned*.

## See also

- `skills/specializations/planner/SKILL.md` § Numbers and deadlines without a source — the numeric special case (L2) this ladder generalizes to every load-bearing decision/judgment; and its `policy.md` detail.
- [plan-activity-ontology.md](plan-activity-ontology.md) — element 7 (refutable principle: source / confidence / refutation), the per-claim grounding shape the ledger records.
- `CLAUDE.md` § Verify the right axis — arms the reasoning-deliverable provenance gate at `classify --deliverable-kind reasoning`; `scripts/agentctl/README.md` § ledger — the L1 closure mechanism.
- [[question-provenance-gate]] — the plan-approval-axis twin of this resolution-axis ledger: the `premise` plugin applies the same rule/perception split (recorded questions dispositioned = form; which questions exist = perception) at plan_approval, and inherits this leaf's RECALL<100% residual verbatim.
