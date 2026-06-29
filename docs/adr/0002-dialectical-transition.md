# ADR-0002 — Dialectical transition: the σ operator and principle revision

- **Status:** Accepted (2026-06-29) — first slice (the σ-sentinel) implemented; the σ operator itself is **deferred behind a pre-registered build-trigger** (see `docs/sigma-build-trigger.md`).
- **Deciders:** system authors (commit-rights holders to this repo)
- **Difficulty removed:** the system can *split* a tension within a fixed vocabulary (critique) and can *aggregate* repeated experience into a principle (induction), but it has **no operator that produces a new higher-level concept out of a contradiction between two existing commitments** — the dialectical transition (Aufhebung: preserve + cancel + lift). This ADR names that operator, shows it already lives implicitly in the system, and records the decision to make it observable now and buildable later.

## Context

ADR-0001 derived the whole architecture from one primitive — **critique**: compare two objects, extract their commonality (the invariant) and their difference (the boundary). That primitive has two existing uses, and **neither is dialectical**:

1. **κ(D) = (I, Δ) — splitting.** "Hold invariant I, change boundary Δ." This is the work of the *understanding* (Verstand): it partitions existing properties into keep/change **inside a fixed vocabulary**. No new third term arises; the dimensionality of the property space is unchanged.
2. **Promotion of experience into a principle (the Rule of Three) — induction.** An ascent, but an aggregative one: see the same commonality three times → generalize. The lift comes from *repetition*, not from *contradiction*.

What is missing is an operator that, faced with two commitments that genuinely contradict, **adds a distinction that did not exist in either** and reconciles them in the enriched space. That is the dialectical transition.

**Evidence the move is real (it has already happened once, by hand).** The *difficulty* primitive of ADR-0001 is itself a completed Aufhebung. Before it, the instructions carried separate concepts — bug, stage-result mismatch, edit conflict, lesson; the concept "difficulty" subsumed them, each **preserved** as a moment, **cancelled** as a separate genus, **lifted** into one frame. The result was not *more* rules but *fewer and deeper* ones. The operation this ADR mechanizes already produced the system's own foundation — manually, in the author's head.

## Decision drivers

- **D1** — The two existing engines (κ split, inductive promotion) cannot generate a *new predicate*; some tensions are unsolvable without one.
- **D2** — Any such operator must be grounded in the system's own *difficulty → principle* ontology, not bolted on.
- **D3** — A synthesis operator is powerful and partly non-algorithmic; building it speculatively risks false universals. The decision to build must be **falsifiable in both directions**.
- **D4** — The operational hot path (fast conflict-quenching) must not be slowed to feed slow conceptual growth.

## Decision

### 1. The formal signature: κ stays inside the vocabulary, σ extends it

This is a type distinction, not a rhetorical one.

- **κ** acts on a difficulty `D = (s*, s)` in a *fixed* property space. Dimensionality is unchanged.
- **σ** acts on a *contradiction* between two commitments. Let a commitment be a norm `P : C → A` (context → prescribed action). `P, Q` are in **genuine contradiction** on a region `c ⊆ C` if both are active on `c`, they prescribe incompatible actions (`P(c) ⟂ Q(c)`), and no higher rule adjudicates them.

> **σ(P, Q) = (d, R)**, where `d` is a **new distinction** (a predicate absent from the vocabulary of `P, Q`) and `R` is a higher norm over the enriched space `C × {d}` such that `R` restricted to `d⁺` entails `P` and `R` restricted to `d⁻` entails `Q`.

**The whole difference in one sentence: κ partitions the existing space; σ adds a dimension to it.** The contradiction that was unsolvable in `C` dissolves in `C × {d}` — not because one pole lost, but because an axis appeared on whose two sides both are right. This is the formal signature of Aufhebung: the resolution lives in a space of *higher dimension* than the problem. It is checkable: if no new predicate does the work, it was not σ but κ / a boundary / a victory of one side.

**Canonical worked example — the weight classes.** `P` = "production code → spawn developer"; `Q` = "small changes in-thread". They contradict on "small production edits". The new `d` = *task weight* (lines × files × presence of an architectural decision) — present in neither rule. `R` = "route by weight". `P` and `Q` become the restrictions of `R` to the heavy and light sides of `d`. The new predicate does real behavioural work → this is σ, not a relabeling.

### 2. The contradiction is the appearance of a tier-1 difficulty

A naive reading places the contradiction at the operational level (two rules collided during a task) and concludes it is irreducible to `(s*, s)`. That is a level error. The correct localization is in the **system of principles**, and it is *asymmetric*:

- on one side, the **known** principle `P0`, which entails its plan;
- on the other, the **sought** principle `P1`, which entails the *adequate* plan.

`P0` is actual (we rely on it); `P1` is desired (sought). So `(P1, P0)` is again `(s*, s)` — **a difficulty** — only lifted one tier of generality: its material is principles, not states.

> **D⁽¹⁾ = (P1, P0).**

This dissolves the objection that the `(s*, s)` primitive is blind to contradiction. It is blind to contradiction *at tier 0* — but a tier-0 contradiction is only the *appearance* of a tier-1 difficulty. Two principles firing apart on `c` are the symptom; the difficulty itself is that the principle-system Σ does not generate an adequate plan for `c` while a sought Σ′ — carrying a new distinction — does. Lift the primitive one tier and it sees. (The irony: the unification "everything is a difficulty" was bought by assuming `s*` is coherent; that very assumption makes the primitive blind to contradiction at tier 0. The Aufhebung at level *n* produces the one-sidedness that drives level *n+1*.)

### 3. The structure of the sought principle: correspondence + experimentum crucis

`P1` must:

- reproduce `P0` on all prior experience `E = {e₁, …, e_{n-1}}` — the **correspondence principle** (the moment of *preservation*);
- diverge from `P0` only on the last precedent `e_n` (the moment of *cancellation*);
- thereby widen coverage (the moment of *lift*).

`e_n` is an **experimentum crucis**: the single precedent that discriminates two theories agreeing everywhere else. The four tests for a genuine third term map onto theory choice one-to-one:

| Test | Content | Mechanizability |
|---|---|---|
| **Coverage** (preservation) | `P1` reproduces `P0` on `E` | yes — behaviour replay |
| **Irreducibility** (cancels one-sidedness) | `d` is inexpressible in the old vocabulary; a skeptic cannot restate `(d, R)` as "`P, Q` + boolean conditions" | semi — adversarial check |
| **Compression** (lift) | `(d, R)` removes ≥ N exception-clauses; the ruleset is *smaller and deeper* | yes — line/clause delta |
| **Productivity** | `d` correctly decides future `e_{n+1}, …` | weakly — deferred in time |

The productivity test is the real discriminator and the most Hegelian (the concrete-universal *generates* new cases from itself). It is also the source of a fundamental limit (below).

### 4. One primitive, one continuum, two ascent engines

> The system has **one primitive** (difficulty) and **one continuum** (generality). Difficulties recur at every tier. At tier 0 we remove them by **planning**; at tier ≥ 1 by **principle revision (σ)**. Induction ascends the continuum from **repetition**; σ ascends from a **refuting precedent**. It is the same act — removing a difficulty — at different tiers.

| Engine | Trigger | Existing | Missing |
|---|---|---|---|
| Induction | repetition of one difficulty | `promote-scan` (Rule of Three) | — |
| **Dialectic (σ)** | a refuting precedent | the organs exist (§5) | the tier verdict + a route into a σ registry |

A new principle's `induced_from` points, in the first case, at a *recurring cluster*; in the second, at the *refuted predecessor* `P0`.

### 5. Failure attribution: who decides "principle, not execution"

The organs already exist in `overcome-difficulty`:

- **investigation** — distributed, per-stage result control: *at which step* the plan diverged from reality;
- **critique** — *the nature* of the mismatch (today: extracting `I, Δ`).

To them is added one determination critique must make explicit — the **fidelity check**:

> Was the activity a *faithful realization* of what the principle-via-plan prescribed?
> — faithfully realized and it still failed → the **principle** is refuted (tier 1, σ);
> — realized incorrectly → **execution** slipped (tier 0, retry/replan).

This is the Duhem-Quine caveat and the experimenter's regress: a botched experiment does not refute the theory. Only a **clean** failure (competent execution under satisfied conditions) rises to a refutation of the principle.

**The data for fidelity already exist — the 8 elements of the plan-activity ontology** (`memory-global/leaves/plan-activity-ontology.md`). Attribution is a descent through the elements:

| Plan element | Failure means | Tier |
|---|---|---|
| Order | reorder | 0 |
| Material → Result | material/result mismatch | 0 |
| Means | a means was absent | 0 |
| Method | method misapplied | 0 |
| Conditions / invariants | an invariant was violated | 0 |
| Actor / capability | actor incapable → reroute | 0 |
| **Refutable principle (8)** | **elements 1–7 are clean, yet Result ≠ Expected OR the control criterion itself was wrong** | **1** |

**The mark of a refuted principle: the failure survived the exhaustion of all tier-0 explanations.** A principle refutation is the **residual** after the executional hypotheses are stripped away (Lakatos: blame the core last; Quine: the periphery first). This gives a *structural* anti-thrashing filter beneath the numerical Rule of Three.

**Two divergence detectors catch the two tiers:**

1. **Per-stage control** (investigation) → an intra-plan divergence, mostly tier 0; it also *names the suspect sub-principle* that normed the slipped sub-activity.
2. **Final verification against the original difficulty** `D = (s*, s)` → the case where every stage passed, the plan reached its *declared* Result, and the original difficulty **still stands**. The gap between the *declared* Result and the *actually desired* `s*` is **the purest tier-1 signature**: "flawless execution, difficulty unresolved." That is the crucial precedent `e_n`.

**Tie to the gates.** `agentctl verify-final` yields "all stages PASSED + Final verification", but **resolved** requires the *user's* confirmation. "Green on every internal criterion, yet the user will not confirm" is the operational signal of a principle refutation. Today such a case is pushed back into `overcome-difficulty` as one more tier-0 replan — that is exactly the shortfall this model names.

### 6. The decision proper

Adopt the model above (the difficulty-primitive-by-tier; κ vs σ; the correspondence principle + experimentum crucis; the four tests; the two divergence detectors). Then, on building σ itself:

- **Build the σ-sentinel now, defer σ.** The decision "build σ or not" is itself a tier-1 difficulty with `P0` = "the manual path (κ/critique + manual `promote-scan`) suffices". Rather than decide by intuition, **pre-register the experimentum crucis that would refute `P0`** — the three build-trigger conditions and the kill-condition recorded in the operational distillation `docs/sigma-build-trigger.md`.
- The **empirical probe** (below) shows the σ-fuel is real but **rare in product work and concentrated in self-improvement** — so σ is, in effect, the operator *of self-improvement itself*, and the manual path handles the rare product case. This justifies deferral, not abandonment.
- When a trigger fires, build is **incremental** — only the seam that the fired condition lights up (§ the three seams), never the whole rig at once.

### 7. Two clocks (the safety/growth tension)

There is a real project tension: *safety* wants to **quench** contradictions fast (gates, eval) — which is anti-dialectical; *growth* wants to **hold** a productive contradiction, let it accumulate, and synthesize when ripe. The resolution is **two clocks**: quench *operationally* fast (pick a side to act now — a local "one side wins / boundary"), but *log the contradiction* for slow synthesis. **Act with the understanding, grow with reason.** The contradiction ledger is then not a bug-log but a deliberately maintained **reservoir of tension** — and it must not be a second store, only a new field/type on the existing experience record (so `promote-scan` can cluster refutations the way it clusters repetitions).

## Considered options

- **A. Do nothing — keep κ + inductive promotion only.** Cannot generate a new predicate; the residual class of principle-refutations stays flattened into tier-0 replans. Rejected as the status quo this ADR is about.
- **B. Build σ now (full rig).** The probe shows the fuel is rare in product work; calibrating thresholds with no data and shipping a partly non-algorithmic operator speculatively risks false universals. Rejected as premature.
- **C. Record the model + build the σ-sentinel, defer σ behind a pre-registered trigger (chosen).** Makes the build decision falsifiable in both directions (the trigger fires → build the lit seam; the kill-condition elapses → `P0` corroborated, archive), at near-zero cost, without perturbing the hot path.

## Consequences

**Positive**

- The model is recorded as a citable decision; the operational doc and the σ-sentinel code are its distillations (mirroring ADR-0001's ADR-plus-distillations shape).
- The build decision is falsifiable in both directions; no speculative machinery ships.
- The σ-sentinel's tag accumulates exactly the baseline data the deferred signals (B, the dear-C discriminator) will later need.

**Negative / risks**

- σ remains un-built; the rare product-side tier-1 case stays on the manual path until a trigger fires (accepted — the manual path demonstrably handled the one observed instance).
- Fidelity is itself a critique judgment (the experimenter's regress is real); the engine guarantees critique is *performed*, not that its *verdict is correct*. This is the incompressible cognitive core and is correctly located in the act of critique, not in code.
- σ has a boundary: it works on contradictions that are **artifacts of an impoverished vocabulary** (removed by enrichment), **not** on **structural trade-offs** (irreducible — e.g. the `coordinator-objective` axes are constitutively opposed; there is no `d` making them consistent). Forcing a synthesis where the trade-off is structural manufactures a *false universal* that hides the real trade-off — worse than an honest boundary. Telling vocabulary from structure is itself a judgment, the last non-automatable node before σ.

## Empirical probe (2026-06-29)

Two surveys with one classifier (tier 0 = state/execution; tier 1 = refuted principle / "clean run, difficulty stands"):

| Corpus | Tier-1 | Share | Character |
|---|---|---|---|
| Self-improvement (`memory-global/leaves/experience`, 17) | 11 | ~65 % | biased: the work *is* principle design |
| Product task-work (deepagent, 14) | 1 (+1 ambiguous) | ~7 % (max 14 %) | unbiased: tier-0 dominates (a missing platform/infra fact) |

**Aufhebung signature in line count:** `CLAUDE.md` over 24–27 June went `284 → 200` (−30 %) — the ruleset compression landed exactly on the peak of principle work. Aufhebung happens — but by hand.

**Conclusion.** The fuel is real but **rare in task-work and concentrated in self-improvement** — σ is essentially the operator of self-improvement itself. The single product tier-1 case (`deepagent-415`: static checks green, runtime fails from a frozen porto layer; `P0` "static checks suffice" → `P1` "a live E2E is required before module resolution") was **already promoted to a principle by hand** (the "verify the load-bearing axis" rule in `CLAUDE.md`). The manual path works for the rare case → build is deferred until a trigger fires.

## Open questions

1. The exact thresholds for trigger conditions (B) and the dear-(C) discriminator — deferred until the tag accumulates a baseline (calibration-without-data is explicitly avoided; see `docs/sigma-build-trigger.md`).
2. The reflexive horizon — σ taking *operators* (not rules) as input, so that `σ(κ, σ)` would yield a higher-order operator (a system editing its own ways of editing itself). The most powerful and least-constrained part of the design; not to be touched without hard boundaries. Recorded as a horizon, not a plan.

## References

- Source of record: the design reflection of 2026-06-27/29 (consolidated; the Russian original is the working draft, this ADR is its English decision-of-record).
- `docs/adr/0001-consensus-architecture.md` — the critique primitive and the generality gradient this ADR extends with σ.
- `docs/sigma-build-trigger.md` — the operational distillation: P0, the (A)/(B)/(C) build-trigger, the kill-condition, the deferred-increment schedule.
- `memory-global/leaves/plan-activity-ontology.md` — the 8 plan elements that supply the fidelity descent; `memory-global/leaves/coordinator-objective.md` — the structural trade-off axes that bound σ.
- Intellectual lineage: Hegel (Aufhebung, determinate negation, negation of negation, bad infinity, concrete-universal) → Marx → Vygotsky → Leontiev (activity) → Shchedrovitsky (rupture → reflection → norming). Popper/Lakatos/Kuhn/Quine (falsification, protective belt, crucial experiment, underdetermination, "blame the core last"). Peirce (abduction). Duhem-Quine and the experimenter's regress (the competent-experiment condition).
