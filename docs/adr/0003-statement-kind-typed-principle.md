# ADR-0003 — Typed principle: `statement_kind` (`знание`/`сущее` vs `норма`/`должное`)

- **Status:** Accepted (2026-07-13) — implemented: `statement_kind` typed and validated across `agentctl/plan.py`, `agentctl/state.py`, `verify-plan-file.py`; documented in `plan-activity-ontology.md` § Element 7 and `principle-leaf-schema.md`.
- **Deciders:** system authors (commit-rights holders to this repo)
- **Difficulty removed:** element 7 of the plan-activity ontology (the *refutable principle* a stage rests on) was **one undifferentiated "refutable" thing**. But a principle is one of exactly two categories — a `знание` (descriptive claim about how the world is) or a `норма` (prescriptive claim about what should be done to reach a goal) — and the two are refuted on **different terms**. Conflating them lets a category error pass: a norm "refuted by the world", or a knowledge-claim defended by appeal to a goal. The engine had no way to demand the *right* refutation for each kind.

## Context

The four differentiations of the agent norm against the SMD/MMK tradition (Shchedrovitsky's `ММК`, Anisimov's `ММПК`, «Деятельность как таковая») rest on one settled conceptual base:

- There are exactly **two categories**: `знание` (`сущее` / *is*, descriptive, refutable-by-the-world) and `норма` (`должное` / *ought*, prescriptive, adequate-or-not to a goal).
- `принцип` is the **most general member of the norm-series** (`цель→план→программа→метод→подход→принцип`), **not** a third category.
- There is **one** refutation axis, **reflexively** structured: only `знание` is refuted (by the world); a `норма` is shown **inadequate** when a goal it serves is blocked, and by the reflexive figure that inadequacy *is* the discovery that some grounding `знание` (possibly one reflexive level up) was false.

The `Principle` model (`state.py`) carried `statement / source / confidence / refutation`, and both enforcement surfaces (`plan.py` for TOML, `verify-plan-file.py` for prose) validated that shape — but nothing typed *which kind of claim* the principle was, so `refutation` could name a world-test for a norm or a goal-appeal for a knowledge-claim with no complaint.

An earlier framing (`refutation_axis: world | goal`) was **rejected** as wrong: it posited two *symmetric* refutation axes, whereas the tradition holds there is one axis reflexively structured (a blocked goal points back at a false grounding `знание`). That token must not appear.

## Decision drivers

- **D1** — Type the principle so the engine can demand the refutation appropriate to its kind, without conflating world-tests and goal-adequacy.
- **D2** — Reuse the existing `Principle` model and the codebase's grandfather idiom; invent no new validation machinery.
- **D3** — Do not invalidate a single existing plan or leaf. A mandatory field plus a data-migration script rewriting every artifact was **considered and rejected** as disproportionately expensive and risky; grandfathering an optional field is the migration.

## Decision

### 1. `statement_kind: сущее | должное`, optional and grandfathered

`Principle` (`state.py`) gains `statement_kind: str | None = None`. Legal present values are the two `StatementKind` enum members — `сущее` (`знание` / *is*) and `должное` (`норма` / *ought*). `None` is the untouched-legacy default, supplied by the dataclass so every pre-typing plan/JSON reconstructs unchanged (`Principle(**d["principle"])` needs no explicit `.get`). `SCHEMA_VERSION` bumps 14→15.

### 2. Validation fires only when the field is present

- `plan.py` `_validate_substantive_stage`: when `statement_kind` is present it must be a valid `StatementKind` value; absent → grandfathered, no error. Mirrors the existing `Confidence`-enum check shape. `parse_plan` reads the key into `Principle`.
- `verify-plan-file.py`: the prose Principle mirror recognizes an **optional** `statement_kind:` label and validates its value against `{сущее, должное}` when present; absent → no error.

The four required subfields keep their shape and required-ness; the anti-template checks (refutation≠statement, statement≠method) are untouched. `gates.py` is not touched (its purity is preserved).

### 3. The reflexive refutation figure is documented, not two symmetric axes

`plan-activity-ontology.md` § Element 7 and `principle-leaf-schema.md` describe: `сущее` is refuted by the world; `должное` is shown inadequate when a goal it serves is blocked, and by the reflexive figure that inadequacy is the discovery that a grounding `сущее` was false — the response being **reflexive reconstruction** (repair the grounding `знание`, then renorm the `норма`). `принцип` is named as the most general norm-series member, not a third kind.

## Consequences

- Element 7 principles can now be typed; the engine rejects a bogus `statement_kind` value while grandfathering every artifact that omits it.
- This is the **means/principle** application of the shared root *`должное` (`норма`) rests on `сущее` (`знание`)*. The **goal** application of the same root (R2 — `failure_address` routing a goal-failure to a content-fault (`сущее`) or form-fault (`должное`)) reuses this same `StatementKind` enum rather than coining a second one; that mechanization is recorded in a later section / stage of this work.
- Cost paid: one optional field, one enum, three-surface validation, and documentation. No data-migration script; no existing artifact rewritten.

## Refutation of this decision

As a norm (`должное`), this typing is shown **inadequate** if authors cannot reliably decide `сущее` vs `должное` for a real principle (the distinction is not operational), or the field is left blank so universally that grandfathering swallows it. Then the goal "demand the right refutation" is blocked, and reflexively the grounding `знание` "the two kinds are cleanly separable for agent-norm principles" was false — and the field should be dropped or merged back.
