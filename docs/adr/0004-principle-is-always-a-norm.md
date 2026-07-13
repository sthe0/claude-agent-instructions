# ADR-0004 — The principle (element 7) is always a норма; drop `statement_kind`

- **Status:** Accepted (2026-07-14) — supersedes [ADR-0003](0003-statement-kind-typed-principle.md) § 1. Implemented: the `statement_kind` field is **removed** from `agentctl/state.py` (`Principle`), `agentctl/plan.py` (parse + validation), and `verify-plan-file.py` (prose mirror); legacy carriers are grandfathered on load; documented in `plan-activity-ontology.md` § Element 7 and `principle-leaf-schema.md`. <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->
- **Deciders:** system authors (commit-rights holders to this repo)
- **Difficulty removed:** ADR-0003 § 1 typed element 7 (the *refutable principle* a stage rests on) a-priori as `statement_kind: сущее | должное` — as if a principle could be *either* a `знание` or a `норма`. This is a **category error**. `принцип` is the **most general member of the norm-series** (`цель→план→программа→метод→подход→принцип`); it is therefore **always a норма** (`должное`) and can never be a `знание`. A норма is **never checked for truth** — so an a-priori знание-vs-норма tag on the principle types a distinction that does not exist at that place. The fault-character ADR-0003 tried to fix on the principle in advance is real, but it belongs to a **fault**, discovered **post-hoc** by критика at difficulty closure — which is exactly where `failure_address` (R2) routes it, reframed in § 4 below as *which обеспечение was inadequate* (ресурсное vs нормативное), not as an a-priori is/ought tag on the norm.

## Context

The four differentiations of the agent norm against the SMD/MMK tradition (Shchedrovitsky's `ММК`, Anisimov's `ММПК`, «Деятельность как таковая») rest on one settled conceptual base, restated from ADR-0003 § Context but read correctly this time:

- There are exactly **two categories**: `знание` (`сущее` / *is*, descriptive, refuted-by-the-world) and `норма` (`должное` / *ought*, prescriptive, adequate-or-not to a goal).
- `принцип` is the **most general member of the norm-series** (`цель→план→программа→метод→подход→принцип`), **not** a third category — and, crucially, **not a free choice between the two categories either**. Being a member of the norm-series, it *is* a норма. «Норма — тоже ресурс»: the norm is itself a resource the activity draws on, not a truth-apt claim.
- There is **one** refutation axis, **reflexively** structured: only `знание` is refuted (by the world); a `норма` is shown **inadequate** when a goal it serves is blocked, and by the reflexive figure that inadequacy *is* the discovery that some grounding `знание` (possibly one reflexive level up) was false. <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->

ADR-0003 § 1 read this base as licensing a per-principle `сущее`/`должное` choice. But the norm-series membership already fixes the answer: element 7 is a норма, categorically. The place where the fault-kind is genuinely a *decision* is not the principle at plan time — it is the **fault** at closure: when a goal-failure closes a difficulty, was the inadequate **обеспечение** ресурсное (материал/средство) or нормативное (норма/способ)? That routing is precisely `Critique.failure_address` (R2), and it is inherently **post-hoc**. «Норма — тоже ресурс»: both are kinds of обеспечение, two special cases of one act, both reducing reflexively to знание — not an is/ought typing.

The rejected `refutation_axis: world | goal` token from ADR-0003 § Context remains rejected and must not reappear.

## Decision drivers

- **D1** — Stop typing a distinction that does not exist at element 7. A principle is a норма; do not ask the author to tag it знание-vs-норма a-priori.
- **D2** — Preserve everything that *was* correct: the two refutation **modes** (знание refuted by the world; норма shown inadequate when a goal is blocked), the **one reflexive axis**, and the literal figure **reflexive reconstruction**. These describe how a fault surfaces, not two types of principle. <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->
- **D3** — Do not invalidate a single existing plan or leaf. A legacy artifact still carrying `statement_kind` must load unchanged — grandfather the retired key on load; **no** data-migration script.
- **D4** — Keep `failure_address` (R2) as the post-hoc fault-router. Routing a *fault* at closure to the inadequate **обеспечение** (ресурсное vs нормативное) is legitimate and is the correct home for the distinction ADR-0003 § 1 misplaced onto the principle.

## Decision

### 1. Direction A — drop the field

`Principle` (`state.py`) loses `statement_kind` entirely. Its four subfields — `statement / source / confidence / refutation` — keep their shape and required-ness. Reconstruction becomes tolerant: `Principle.from_dict` filters an incoming dict to the known field names, so a legacy plan/JSON/session still carrying `statement_kind` reconstructs unchanged (the retired key is ignored, never re-required). Load-time tolerance **is** the migration (D3). `SCHEMA_VERSION` bumps 17→18.

An alternative — **Direction B**, *repurpose* the field to name the grounding `знание` a norm rests on (instead of typing the norm itself) — was **considered and deferred** to the plan-review, not implemented here. This ADR records only Direction A.

### 2. Validation removed from both surfaces

- `plan.py` `_validate_substantive_stage`: the `StatementKind`-enum check on the principle is deleted; `parse_plan` no longer reads a `statement_kind` key into `Principle`. A legacy TOML principle block still carrying the key parses unchanged (extra keys are ignored).
- `verify-plan-file.py`: the optional prose `statement_kind:` label and its `{сущее, должное}` value check are deleted. A legacy prose plan still carrying the label is tolerated, never enforced.

The four required subfields keep their required-ness; the anti-template checks (refutation≠statement, statement≠method) are untouched. `gates.py` is not touched (its purity is preserved).

### 3. The two refutation modes and the one reflexive axis stay documented

`plan-activity-ontology.md` § Element 7 and `principle-leaf-schema.md` now state that **the principle is always a норма** and carries **no** a-priori kind, while preserving: `знание` is refuted by the world; a `должное` is shown inadequate when a goal it serves is blocked; on the **one reflexive axis** a blocked goal *is* the discovery that a grounding `знание` was false; and the response is **reflexive reconstruction** (repair the grounding знание, then renorm the норма). The literal token `reflexive reconstruction` is retained. `принцип` is named as the most general norm-series member.

### 4. `failure_address` (R2) retained and reframed — which обеспечение was inadequate, post-hoc <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->

The post-hoc fault-routing stands, but the v4 correction **reframes its values**: a затруднение is overcome by fixing its **обеспечение**, so a fault at closure is routed by *which обеспечение was inadequate* — **ресурсное** (материал/средство) or **нормативное** (норма/способ), or explicit `not_applicable`. This is **not** an is/ought (`сущее`/`должное`) typing — that v3 framing is rejected here too: «норма — тоже ресурс», so both routings are special cases of one act and both reduce **reflexively to знание**. `FAILURE_ADDRESS_VALUES` = `("ресурсное", "нормативное", "not_applicable")`, **decoupled** from `StatementKind` — which is now **deleted** from `state.py` (it survived only to lend its values to R2, and no longer does). Enforcement is unchanged: `gates.failure_address_blockers` blocks `replan` at DIAGNOSING closure until a non-None routing is recorded; a legacy record carrying an OLD `сущее`/`должное` value is grandfathered (the gate checks only non-None), and bogus values are rejected at write time by `cmd_critique` + argparse `choices`. Removing the a-priori tag from the principle does **not** touch this post-hoc routing — it removes the duplicate, mistyped copy at plan time and leaves the single correct one at closure. `SCHEMA_VERSION` bumps 18→19.

## Consequences

- Element 7 is no longer mistyped: a principle is a норма by category, not an author-chosen kind. The engine no longer demands (or grandfathers) an a-priori `statement_kind`. <!-- Language exception: SMD source-ontology terms preserved verbatim for traceability. -->
- Every artifact that ever carried `statement_kind` still loads (grandfathered on reconstruction); no artifact is rewritten.
- The fault-kind distinction survives in its correct, post-hoc home (`failure_address`, R2) — reframed to *which обеспечение was inadequate* (ресурсное vs нормативное), un-duplicated and decoupled from the now-deleted `StatementKind`.
- The v3 test asserting the a-priori field (`test_plan_statement_kind.py`) is retired; a regression test (`test_principle_no_statement_kind.py`) asserts the field's absence and the grandfather-on-load path.
- Cost paid: field + validation removal across three surfaces, one from_dict tolerance, doc/ADR updates. No data-migration script.

## Refutation of this decision

As a norm (`должное`), dropping the a-priori type is shown **inadequate** if, in practice, plan authors and the engine *need* to know at plan time which grounding `знание` a principle rests on to demand the right refutation — i.e. if the post-hoc `failure_address` routing at closure proves too late to be useful, and a plan-time hook is genuinely required. Then Direction B (repurpose the field to name the grounding `знание`, not to type the norm) becomes the indicated successor, and the goal "type the distinction at its correct place" would reopen. The reflexive grounding `знание` here is "the сущее/должное distinction has no operational locus at element 7, only at fault-closure"; were that false, this decision should be revisited.
