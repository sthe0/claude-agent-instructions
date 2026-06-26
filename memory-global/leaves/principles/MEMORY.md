# Principles

Generality-graded, provenance-rooted, **refutable** principles induced from recurring difficulties
(ADR-0001 § *Principle as a concept with a generality gradient*). Each leaf carries `schema:
principle/v1` — a statement at its generality level (0 trivial → 3 cross-domain invariant), an
`induced_from` link **down** to the difficulties it generalizes, and a refutation condition
(refutation ≡ generalization). Schema: [principle-leaf-schema.md](../principle-leaf-schema.md).

Sub-index of `memory-global/leaves/principles/`. Pointed at from `memory-global/MEMORY.md`. Not
auto-loaded by the harness. **Consumption:** at a plan's `refutable principle` element the planner
retrieves the relevant principle(s) to ground each stage (retrieval-augmented planning).

## Level 3 — cross-domain invariants

- [Every result is critiqued against its declared result-image](result-checked-against-its-result-image.md) — the critique primitive (compare expected vs actual, extract the difference) applied to one stage; skipping the check is itself a difficulty. ← `coordinator-pitfalls`, `2026-06-24-gate-exemption-is-category-error-for-result-images`.
- [A complete option space is generated from the functional ground](option-space-spans-axes-from-functional-ground.md) — span orthogonal axes (passive/active, batch/continuous, precedence/synthesis); generate options from the difficulty's ground, not a mechanism catalogue; ask what invariant subsumes them. ← `coordinator-pitfalls`, `2026-06-26-critique-primitive-unifies-conflict-and-principle`.

## Level 2 — task-class rules

- [The coordinator executes through specialists](coordinator-executes-through-specialists.md) — achieve production change by dispatch, not direct root edits; direct Bash/Edit/Write on substantive work is a difficulty signal. ← `coordinator-pitfalls`.
